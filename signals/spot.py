"""SPY spot time series: actual close where it exists, parity proxy otherwise.

``underlying_day_bars`` only reaches as far as the equity-aggs entitlement
(~2 years, and the backfill is still filling it). ``atm_term_structure``
reaches 2022-08-31. Realised-vol work needs a single SPY level on every
session, so this reduction prefers the bar close and fills the hole from the
shortest-DTE SPY parity forward.

The proxy is the cash identity, not a yield guess::

    S = F e^{-r T} + PV(dividends in (session, expiry])

``q`` is then :func:`pricing.bsm.resolve_q` of that ``(S, F)`` pair, so
``F = S e^{(r-q)T}`` holds by construction. Shortest DTE is the point: a
1-4 day option almost never contains an ex-date, so ``q`` is 0 and the
proxy is ``F e^{-rT}``.

On overlap sessions ``resid = proxy − actual`` and ``src="bars"`` (the
stored spot is the actual close). Elsewhere ``resid`` is null.

Measured 2022-08-31..2026-09-04 (1007 sessions, 169 overlap — the SPY
day-bar backfill is still filling the ~2-year entitlement window):
median |resid| $0.35, p90 $1.01, max $5.91, lag-1 autocorr −0.09.
That is 11% of the median |daily SPY move|, below
``ROLL_DEBIAS_FRAC`` (0.25), so Stage 1.1 should **not** Roll-debias.
Re-run ``scripts/build_spy_spot.py --force`` after the day-bar backfill
catches up (without ``--force`` already-written parity rows are skipped);
the report is ``_meta/spy_spot_calibration.json``.

Run: ``python -m signals.spot [--date YYYY-MM-DD]`` (default: previous
trading day). Archive + calibration: ``scripts/build_spy_spot.py``.
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from ingest.common import landing, market_gate
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import latest_clean_records, partition_dates, read_partition
from pricing.bsm import resolve_q
from pricing.daycount import (
    DayCountStampError,
    discount_year_fraction,
    require_daycount_stamps,
)

JOB = "spy_spot"
DATASET = "spy_spot"
ROOT = "SPY"
SRC_BARS = "bars"
SRC_PARITY = "parity"
CALIBRATION_NAME = "spy_spot_calibration.json"
# Residual as a fraction of the median |daily SPY move|. Above this, a
# realised-vol estimator that treats the parity-only tail as observed spot
# must Roll-debias rather than ignore the noise (Stage 1.1).
ROLL_DEBIAS_FRAC = 0.25


class SpotError(RuntimeError):
    """Raised when a session has neither a SPY bar nor a usable SPY forward."""


def pv_dividends(
    dividends: Sequence[Mapping[str, Any]],
    session: date,
    expiry: date,
    r: float,
) -> float:
    """Present value as of ``session`` of cash dividends with ex-date in (session, expiry]."""
    total = 0.0
    for rec in dividends:
        raw_ex = rec.get("ex_dividend_date")
        cash = rec.get("cash_amount")
        if raw_ex is None or cash is None:
            continue
        try:
            ex = date.fromisoformat(str(raw_ex)[:10])
            amount = float(cash)
        except (TypeError, ValueError):
            continue
        if amount <= 0 or not (session < ex <= expiry):
            continue
        # Money time: this discounts a dividend, and cash accrues on
        # weekends. Never a session count -- see pricing/daycount.py.
        t = discount_year_fraction(session, ex)
        total += amount * math.exp(-float(r) * t)
    return total


def shortest_spy_term(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Shortest strictly-positive-DTE SPY term-structure row with a forward."""
    eligible: list[dict[str, Any]] = []
    for rec in rows:
        if str(rec.get("underlying") or "") != ROOT:
            continue
        dte = rec.get("dte")
        fwd = rec.get("forward")
        rate = rec.get("rate")
        t_years = rec.get("t_years")
        if dte is None or int(dte) <= 0:
            continue
        if fwd is None or rate is None or t_years is None:
            continue
        if not (math.isfinite(float(fwd)) and float(fwd) > 0):
            continue
        if not math.isfinite(float(rate)) or not math.isfinite(float(t_years)):
            continue
        eligible.append(dict(rec))
    if not eligible:
        return None
    try:
        require_daycount_stamps(eligible, context="atm_term_structure")
    except DayCountStampError as exc:
        raise SpotError(str(exc)) from exc
    return min(eligible, key=lambda r: int(r["dte"]))


def proxy_spot(
    term: Mapping[str, Any],
    dividends: Sequence[Mapping[str, Any]],
    session: date,
) -> tuple[float, float]:
    """``(S, q)`` from the shortest-DTE forward and the dividend stream.

    ``S = F e^{-rT} + I``, then ``q = resolve_q(S, T, r, F=F)`` so the
    continuous-yield form ``S = F e^{-(r-q)T}`` is the same number.
    """
    F = float(term["forward"])
    r = float(term["rate"])
    expiry = date.fromisoformat(str(term["expiration_date"])[:10])
    # Money time, not the row's vol-time ``t_years``. After the hybrid default
    # flip those two diverge, and using sessions/252 here would silently
    # shorten the discount -- the mistake finding 3 exists to prevent.
    T = discount_year_fraction(session, expiry)
    income = pv_dividends(dividends, session, expiry, r)
    if T <= 0:
        return F + income, 0.0
    S = F * math.exp(-r * T) + income
    if S <= 0 or not math.isfinite(S):
        raise SpotError(f"non-positive proxy spot {S} on {session} from F={F}")
    q = float(resolve_q(S, T, r, F=F))
    return S, q


def spy_close(rows: Sequence[Mapping[str, Any]]) -> float | None:
    """SPY close from an ``underlying_day_bars`` partition, or None."""
    for rec in rows:
        if rec.get("ticker") != ROOT:
            continue
        close = rec.get("close")
        if close is None:
            return None
        value = float(close)
        return value if math.isfinite(value) and value > 0 else None
    return None


def assemble(
    session: date,
    *,
    close: float | None,
    term: Mapping[str, Any] | None,
    dividends: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """One ``spy_spot`` row, or None when neither source exists."""
    proxy: float | None = None
    q: float | None = None
    if term is not None:
        proxy, q = proxy_spot(term, dividends, session)
    if close is None and proxy is None:
        return None
    if close is not None:
        src = SRC_BARS
        spot = close
        resid = (proxy - close) if proxy is not None else None
    else:
        src = SRC_PARITY
        spot = float(proxy)  # term was not None
        resid = None
    return {
        "date": session.isoformat(),
        "spot": spot,
        "forward": float(term["forward"]) if term is not None else None,
        "dte": int(term["dte"]) if term is not None else None,
        "rate": float(term["rate"]) if term is not None else None,
        "q": q,
        "src": src,
        "resid": resid,
    }


def _median(values: list[float]) -> float | None:
    """Statistical median: average the two middle values when ``n`` is even.

    Matches :func:`ingest.jobs._median` / :func:`pricing.drift_check._median`.
    Taking ``ordered[n // 2]`` alone is the upper middle and would pin the
    four-residual overlap test at 0.2 instead of 0.15.
    """
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]


def _lag1_autocorr(values: list[float]) -> float | None:
    if len(values) < 3:
        return None
    x = values[:-1]
    y = values[1:]
    n = len(x)
    mx = sum(x) / n
    my = sum(y) / n
    var_x = sum((a - mx) ** 2 for a in x)
    var_y = sum((b - my) ** 2 for b in y)
    if var_x <= 0 or var_y <= 0:
        return None
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    return cov / math.sqrt(var_x * var_y)


def calibrate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Overlap residual stats + whether Stage 1.1 needs a Roll-style debias."""
    ordered = sorted(rows, key=lambda r: str(r.get("date") or ""))
    overlap = [r for r in ordered if r.get("resid") is not None]
    abs_err = [abs(float(r["resid"])) for r in overlap]
    signed = [float(r["resid"]) for r in overlap]
    # Only consecutive *calendar-adjacent bar rows* count as a daily move.
    # Dropping parity fills first would treat two bars with a hole between
    # them as a one-day change and inflate median_abs_daily_move.
    moves: list[float] = []
    for i in range(1, len(ordered)):
        prev, cur = ordered[i - 1], ordered[i]
        if (
            prev.get("src") == SRC_BARS
            and cur.get("src") == SRC_BARS
            and prev.get("spot") is not None
            and cur.get("spot") is not None
        ):
            moves.append(abs(float(cur["spot"]) - float(prev["spot"])))
    median_err = _median(abs_err)
    median_move = _median(moves)
    ratio = (
        (median_err / median_move)
        if median_err is not None and median_move is not None and median_move > 0
        else None
    )
    roll = bool(ratio is not None and ratio >= ROLL_DEBIAS_FRAC)
    return {
        "n_rows": len(ordered),
        "n_bars": sum(1 for r in ordered if r.get("src") == SRC_BARS),
        "n_parity": sum(1 for r in ordered if r.get("src") == SRC_PARITY),
        "n_overlap": len(overlap),
        "median_abs_error": median_err,
        "p90_abs_error": _p90(abs_err),
        "max_abs_error": max(abs_err) if abs_err else None,
        "autocorr_lag1": _lag1_autocorr(signed),
        "median_abs_daily_move": median_move,
        "error_vs_move": ratio,
        "roll_debias_frac": ROLL_DEBIAS_FRAC,
        "roll_debias_required": roll,
        "units": "USD (spot dollars)",
    }


def write_row(settings: Settings, d: date, row: dict[str, Any]) -> Path:
    """Write one session, replacing this job's previous output."""
    prior = landing.clean_files(DATASET, d, JOB, settings.data_root)
    path = landing.write_clean(
        DATASET, d, [row], job=JOB, data_root=settings.data_root
    )
    if prior:
        landing.quarantine_prior(DATASET, d, JOB, settings.data_root, only=prior)
    return path


def write_calibration(
    settings: Settings, report: Mapping[str, Any]
) -> Path:
    path = landing.meta_path(CALIBRATION_NAME, data_root=settings.data_root)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path


def rewrite_calibration(settings: Settings) -> dict[str, Any]:
    """Re-read every landed ``spy_spot`` partition and rewrite the report."""
    rows: list[dict[str, Any]] = []
    for d in partition_dates(settings, DATASET):
        rows.extend(read_partition(settings, DATASET, d))
    report = calibrate(rows)
    write_calibration(settings, report)
    return report


def load_dividends(
    settings: Settings,
    on_or_before: date | None = None,
) -> list[dict[str, Any]]:
    """One full-history SPY dividend snapshot.

    Each ``dividends_sync`` partition is the whole stream, not that day's
    events. ``on_or_before`` is the scheduled job's as-of. If no partition
    exists at or before that date — every archive session before the first
    sync — fall back to the latest snapshot so ``pv_dividends`` still sees
    historical ex-dates.
    """
    if on_or_before is not None:
        rows = latest_clean_records(settings, "dividends", on_or_before)
        if rows:
            return rows
    dates = partition_dates(settings, "dividends")
    if not dates:
        return []
    return read_partition(settings, "dividends", dates[-1])


def build_for_date(
    settings: Settings,
    d: date,
    *,
    dividends: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Read the day's bars + term structure + a dividend snapshot → one row.

    Pass ``dividends`` to reuse one snapshot across an archive walk. The
    scheduled job leaves it ``None`` and takes the as-of snapshot, falling
    back to the latest if this date predates the first sync.
    """
    bars = read_partition(settings, "underlying_day_bars", d)
    terms = read_partition(settings, "atm_term_structure", d)
    if dividends is None:
        dividends = load_dividends(settings, on_or_before=d)
    return assemble(
        d,
        close=spy_close(bars),
        term=shortest_spy_term(terms),
        dividends=dividends,
    )


def _main_fn(args, settings: Settings, logger: JsonlLogger):
    d = date.fromisoformat(args.date)
    row = build_for_date(settings, d)
    if row is None:
        raise SpotError(
            f"no spy_spot for {d}: no SPY row in underlying_day_bars and no "
            "positive-DTE SPY row in atm_term_structure"
        )
    logger.log(
        "spy_spot",
        date=row["date"],
        src=row["src"],
        spot=row["spot"],
        resid=row["resid"],
        dte=row["dte"],
    )
    print(
        f"PASS  {row['date']}  src={row['src']}  spot={row['spot']:.4f}  "
        f"resid={row['resid']}  dte={row['dte']}",
        file=sys.stderr,
    )
    if not args.dry_run:
        path = write_row(settings, d, row)
        print(f"PASS  wrote {path}", file=sys.stderr)
    return {"rows": 1, "src": row["src"]}


def main(argv: list[str] | None = None) -> int:
    """CLI for the scheduled T-1 run; exits 0 on success, 1 on failure."""
    argv = list(argv) if argv is not None else sys.argv[1:]
    if not any(a == "--date" or a.startswith("--date=") for a in argv):
        prev = market_gate.previous_trading_day(market_gate.today_et())
        argv += ["--date", prev.isoformat()]
    return run_job(JOB, _main_fn, argv)


if __name__ == "__main__":
    raise SystemExit(main())
