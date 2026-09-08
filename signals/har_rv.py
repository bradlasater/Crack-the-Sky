"""HAR-RV baseline realised-variance forecast on the ``spy_spot`` series.

PLAN.md week-1 item 4. The input is :mod:`signals.spot`'s continuous SPY
series (bar close where it exists, shortest-DTE parity proxy otherwise),
taken as observed spot with **no Roll debias** -- the Stage 1.1 calibration
measured the parity-proxy noise at 11% of the median daily move, under the
0.25 debias threshold (``_meta/spy_spot_calibration.json``).

**Model.** Corsi's HAR cascade in logs. A daily series supports one RV
proxy, the squared close-to-close log return::

    rv_t = (ln S_t - ln S_{t-1})^2

    log mean(rv_{t+1..t+h}) = b0 + b_d log rv_t
                            + b_w log mean(rv_{t-4..t})
                            + b_m log mean(rv_{t-21..t}) + e

Both sides are logs of average RV over a window, so the equation is
dimensionally consistent and forecasts exponentiate back to variance.
Fitting in logs rather than levels keeps the heavy right tail of RV from
dominating the loss and makes the residual band multiplicative -- a
distribution, not a point estimate, which is what the diagram asks for.

**Horizons.** Sessions, matched to the 5-45 DTE book: 3, 5, 10, 21 and 32
sessions are ~4, 7, 14, 30 and 45 calendar days. One row per (date,
horizon).

**Uncertainty.** Per (origin, horizon) the band is the in-sample residual
sd of that origin's own fit, ``sigma = sqrt(RSS / (n - 4))``, read as a
lognormal distribution of the average daily RV over the horizon:
``log_rv_mean`` / ``log_rv_sd`` carry the whole distribution; ``rv_daily``
is its mean (with the sigma^2/2 Jensen correction, so it is an E[rv], not
exp(E[log rv])); ``vol_ann`` annualises it and ``vol_ann_p10`` /
``vol_ann_p90`` are the 80% band. In-sample residual sd slightly
understates out-of-sample error -- acceptable for a baseline the
walk-forward backtester will grade anyway.

**Point-in-time discipline.** The fit for origin ``t`` is an expanding
walk-forward: training rows are origins ``j`` whose *entire* target window
lies at or before ``t`` (``j + h <= t``), so no forecast touches data after
its origin. ``forecast_rows`` is written so truncating the series at ``t``
cannot change the row for ``t`` -- there is a test pinning exactly that.
The origin-``d`` row is computable after ``d``'s close, i.e. it is the
forecast available on the morning of the next session, the same T-1
convention as ``spy_spot`` and ``atm_term_structure``.

**Gaps.** ``rv_t`` is only defined between sessions adjacent on the trading
calendar (``_meta/trading_days.json``; without it, consecutive rows are
assumed adjacent). A missing session makes the surrounding rv null, and any
feature or target window containing a null is unusable -- a hole shortens
the training set rather than fabricating a multi-day "daily" return.

Run: ``python -m signals.har_rv [--date YYYY-MM-DD]`` (default: previous
trading day). Archive: ``scripts/build_rv_forecast.py``.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from ingest.common import landing, market_gate
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import partition_dates, read_partition
from signals.spot import DATASET as SPOT_DATASET

JOB = "rv_forecast"
DATASET = "rv_forecast"

# Corsi windows in sessions: daily / weekly / monthly.
WIN_D, WIN_W, WIN_M = 1, 5, 22
# Forecast horizons in sessions, spanning the 5-45 DTE book
# (3/5/10/21/32 sessions ~= 4/7/14/30/45 calendar days).
HORIZONS = (3, 5, 10, 21, 32)
# Fewer complete training observations than ~3 months of sessions and the
# four-parameter fit is noise; earlier origins get no row.
MIN_TRAIN_ROWS = 63
# 80% band: p10/p90 of the lognormal forecast distribution.
Z_BAND = 1.2815515655446004
SESSIONS_PER_YEAR = 252


class RvForecastError(RuntimeError):
    """Raised when a session yields no forecast rows (short history, gaps)."""


def realized_variances(
    rows: Sequence[Mapping[str, Any]],
    calendar: Mapping[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """``[{date, rv}]`` in session order; rv is the squared log return.

    The first row and any row whose previous *calendar* session is absent
    get ``rv=None`` -- across a gap the squared move is a multi-day return,
    not a daily one. Without a calendar, consecutive rows are assumed to be
    adjacent sessions.
    """
    ordered = sorted(
        (r for r in rows if r.get("date") and r.get("spot")),
        key=lambda r: str(r["date"]),
    )
    session_order: dict[str, str] = {}
    if calendar:
        days = sorted(d for d, ok in calendar.items() if ok)
        session_order = dict(zip(days, days[1:], strict=False))
    out: list[dict[str, Any]] = []
    prev: tuple[str, float] | None = None
    for rec in ordered:
        day = str(rec["date"])
        spot = float(rec["spot"])
        rv: float | None = None
        if prev is not None and math.isfinite(spot) and spot > 0:
            prev_day, prev_spot = prev
            adjacent = (
                session_order.get(prev_day) == day if session_order else True
            )
            if adjacent:
                ret = math.log(spot / prev_spot)
                rv = ret * ret
        out.append({"date": day, "rv": rv})
        prev = (day, spot)
    return out


def _window_mean(rv: np.ndarray, end: int, length: int) -> float | None:
    """Mean of rv[end-length+1 .. end], or None when the window has a gap."""
    if end - length + 1 < 0:
        return None
    window = rv[end - length + 1 : end + 1]
    if not np.isfinite(window).all():
        return None
    return float(window.mean())


def har_features(rv: np.ndarray, i: int) -> np.ndarray | None:
    """``[1, log rv_d, log rv_w, log rv_m]`` at origin ``i``, or None."""
    daily = _window_mean(rv, i, WIN_D)
    weekly = _window_mean(rv, i, WIN_W)
    monthly = _window_mean(rv, i, WIN_M)
    if daily is None or weekly is None or monthly is None:
        return None
    if daily <= 0 or weekly <= 0 or monthly <= 0:
        # rv == 0 means two identical prints -- a stale close, not a market
        # that moved exactly zero. log(0) is not a feature; drop the origin.
        return None
    return np.array([1.0, math.log(daily), math.log(weekly), math.log(monthly)])


def _target(rv: np.ndarray, j: int, h: int) -> float | None:
    """log mean(rv[j+1 .. j+h]), or None when the window has a gap."""
    if j + h >= len(rv):
        return None
    window = rv[j + 1 : j + h + 1]
    if not np.isfinite(window).all():
        return None
    mean = float(window.mean())
    return math.log(mean) if mean > 0 else None


def _fit(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float]:
    """OLS coefficients and residual sd (ddof = n - p) of one HAR fit."""
    beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = len(y) - X.shape[1]
    sigma = math.sqrt(float(resid @ resid) / dof) if dof > 0 else 0.0
    return beta, sigma


def _forecast_row(
    day: str, h: int, x: np.ndarray, beta: np.ndarray, sigma: float, n: int
) -> dict[str, Any]:
    mu = float(x @ beta)
    rv_daily = math.exp(mu + 0.5 * sigma * sigma)  # lognormal mean: E[rv]
    var_ann = SESSIONS_PER_YEAR * rv_daily
    lo = math.exp(mu - Z_BAND * sigma)
    hi = math.exp(mu + Z_BAND * sigma)
    return {
        "date": day,
        "horizon": h,
        "log_rv_mean": mu,
        "log_rv_sd": sigma,
        "rv_daily": rv_daily,
        "vol_ann": math.sqrt(var_ann),
        "vol_ann_p10": math.sqrt(SESSIONS_PER_YEAR * lo),
        "vol_ann_p90": math.sqrt(SESSIONS_PER_YEAR * hi),
        "n_train": n,
    }


def forecast_rows(
    series: Sequence[Mapping[str, Any]],
    horizons: Sequence[int] = HORIZONS,
    min_train: int = MIN_TRAIN_ROWS,
) -> list[dict[str, Any]]:
    """Walk-forward HAR-RV forecast rows for every origin in ``series``.

    ``series`` is :func:`realized_variances` output. The fit at origin ``i``
    for horizon ``h`` trains only on origins ``j <= i - h`` -- origins whose
    whole target window ended at or before ``i`` -- so the row for a date is
    computable from data available at that date's close. Origins with
    incomplete features, or fewer than ``min_train`` complete training rows,
    get no row.
    """
    days = [str(r["date"]) for r in series]
    rv = np.array(
        [r["rv"] if r["rv"] is not None else math.nan for r in series],
        dtype=float,
    )
    n = len(series)
    feats: list[np.ndarray | None] = [har_features(rv, i) for i in range(n)]
    feat_ok = np.array([f is not None for f in feats])
    idx = np.arange(n)
    out: list[dict[str, Any]] = []
    for h in horizons:
        targets = np.array(
            [
                t if (t := _target(rv, j, h)) is not None else math.nan
                for j in range(n)
            ]
        )
        for i in range(n):
            if not feat_ok[i]:
                continue
            x = feats[i]
            assert x is not None  # feat_ok[i]
            # j + h <= i: the training target must end at or before i.
            train = feat_ok & np.isfinite(targets) & (idx + h <= i)
            if int(train.sum()) < min_train:
                continue
            X = np.stack([feats[j] for j in idx[train]])  # type: ignore[arg-type]
            beta, sigma = _fit(X, targets[train])
            out.append(_forecast_row(days[i], h, x, beta, sigma, int(train.sum())))
    out.sort(key=lambda r: (r["date"], r["horizon"]))
    return out


def read_series(
    settings: Settings, on_or_before: date | None = None
) -> tuple[list[dict[str, Any]], dict[str, bool]]:
    """Every landed ``spy_spot`` row at or before a date, plus the calendar."""
    calendar = market_gate.load_calendar(settings.data_root)
    rows: list[dict[str, Any]] = []
    for d in partition_dates(settings, SPOT_DATASET):
        if on_or_before is not None and d > on_or_before:
            continue
        rows.extend(read_partition(settings, SPOT_DATASET, d))
    return rows, calendar


def build_for_date(
    settings: Settings,
    d: date,
    horizons: Sequence[int] = HORIZONS,
) -> list[dict[str, Any]]:
    """Forecast rows whose origin is ``d``, from partitions at or before it.

    Reads only partitions ``<= d``; combined with the walk-forward fit that
    makes the row independent of anything landed later, which is the
    point-in-time guarantee the week-2 feature job and backtester rely on.
    """
    rows, calendar = read_series(settings, on_or_before=d)
    series = realized_variances(rows, calendar)
    out = [
        r
        for r in forecast_rows(series, horizons)
        if r["date"] == d.isoformat()
    ]
    return out


def write_rows(settings: Settings, d: date, rows: list[dict[str, Any]]) -> Path:
    """Write one origin's rows, replacing this job's previous output."""
    prior = landing.clean_files(DATASET, d, JOB, settings.data_root)
    path = landing.write_clean(DATASET, d, rows, job=JOB, data_root=settings.data_root)
    if prior:
        landing.quarantine_prior(DATASET, d, JOB, settings.data_root, only=prior)
    return path


def _main_fn(args, settings: Settings, logger: JsonlLogger):
    d = date.fromisoformat(args.date)
    rows = build_for_date(settings, d)
    if not rows:
        raise RvForecastError(
            f"no rv_forecast for {d}: no spy_spot history at or before it, or "
            f"fewer than {MIN_TRAIN_ROWS} complete training rows"
        )
    logger.log(
        "rv_forecast",
        date=d.isoformat(),
        horizons=len(rows),
        n_train=rows[0]["n_train"],
    )
    for r in rows:
        print(
            f"PASS  {r['date']}  h={r['horizon']:>2}  "
            f"vol_ann={r['vol_ann']:.4f}  "
            f"[{r['vol_ann_p10']:.4f}, {r['vol_ann_p90']:.4f}]  "
            f"n_train={r['n_train']}",
            file=sys.stderr,
        )
    if not args.dry_run:
        path = write_rows(settings, d, rows)
        print(f"PASS  wrote {path}", file=sys.stderr)
    return {"rows": len(rows), "n_train": rows[0]["n_train"]}


def main(argv: list[str] | None = None) -> int:
    """CLI for the scheduled T-1 run; exits 0 on success, 1 on failure.

    ``--date`` defaults to the previous trading day, the convention every
    T-1 reduction follows. Bulk history does not come through here --
    ``scripts/build_rv_forecast.py`` fits the whole archive in one pass.
    """
    argv = list(argv) if argv is not None else sys.argv[1:]
    if not any(a == "--date" or a.startswith("--date=") for a in argv):
        prev = market_gate.previous_trading_day(market_gate.today_et())
        argv += ["--date", prev.isoformat()]
    return run_job(JOB, _main_fn, argv)


if __name__ == "__main__":
    raise SystemExit(main())
