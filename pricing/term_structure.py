"""ATM implied-vol term structure, one row per (date, root, expiry).

Everything upstream of this stops at "the IV of one contract is correct". A
5-45 DTE book is not traded off single contracts -- it is traded off the level
and slope of the ATM curve, and off implied against forecast realised. Neither
is queryable until the per-contract data is reduced to a term structure, which
is what this builds.

**Why day bars and not snapshots.** ``option_snapshots`` carries strike,
expiry and open interest already, and would be the obvious source -- but it
only exists from the day the live sweep started. ``option_day_bars`` goes back
to 2022-08-31, has no contract terms at all, and is therefore the only source
that yields a history worth fitting anything to. The terms come back out of
the OPRA symbol via :func:`ingest.jobs.parse_option_ticker`.

**The forward is recovered from parity, not from a spot.** No index level is
entitled on this tier (``I:SPX`` is a 403), so ``forward_from_parity`` takes
the strike minimising ``|C - P|`` and returns ``F = K + e^{rT}(C - P)``. For
VIX this is the VX future of that expiry rather than a spot VIX, which is the
correct object for a term structure -- see that function's note.

**ATM is the strike nearest the forward**, with both legs inverted and
averaged. Note what the two legs do and do not tell you: when that strike is
also the one parity chose, ``C - P = (F - K)e^{-rT}`` holds by construction
and the legs invert to the same vol -- agreement there is a tautology, not
evidence. The strikes differ often enough (parity minimises ``|C - P|``,
this minimises ``|K - F|``) that the spread is informative when it is
non-zero, so both legs are written out rather than the mean alone.

Pricing is done in the forward measure -- ``S = F`` with ``q`` implied from
``F``, which reduces BSM to Black-76 -- because a spot and a dividend yield
are exactly what this tier does not provide.

Run: ``python -m pricing.term_structure --date YYYY-MM-DD [--root SPXW ...]``
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from ingest.common import landing
from ingest.common.config import Settings
from ingest.common.rates import RateCurveError, load_curve, rate_for
from ingest.jobs import OPTION_ROOTS, forward_from_parity, parse_option_ticker
from pricing.iv import implied_vol

DATASET = "atm_term_structure"
SRC = "day_bars"

# ACT/365, matching pricing/conventions. 252 is a constant elsewhere in the
# repo, not a calendar, so nothing here is trading-day aware.
DAYS_PER_YEAR = 365.0


def _bars_to_chain(rows: list[dict[str, Any]], root: str) -> list[dict[str, Any]]:
    """Day-bar rows -> snapshot-shaped records for ``forward_from_parity``.

    Only the four fields parity reads are populated. Reusing that function
    rather than re-deriving F keeps one definition of the forward in the repo,
    which matters because the drift canary checks against it.
    """
    out = []
    for row in rows:
        terms = parse_option_ticker(row.get("ticker"))
        if terms is None or terms["root"] != root:
            continue
        close = row.get("close")
        if close is None or float(close) <= 0:
            continue
        out.append({
            "details_expiration_date": terms["expiration_date"],
            "details_strike_price": terms["strike"],
            "details_contract_type": terms["contract_type"],
            "day_close": float(close),
            "underlying_ticker": root,
            "day_last_updated_ns": row.get("window_end_ns") or 0,
        })
    return out


def _legs_by_expiry(chain: list[dict[str, Any]]) -> dict[str, dict[float, dict[str, float]]]:
    """``{expiry: {strike: {'call': px, 'put': px}}}`` for ATM lookup."""
    out: dict[str, dict[float, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for rec in chain:
        out[rec["details_expiration_date"]][rec["details_strike_price"]][
            rec["details_contract_type"]
        ] = rec["day_close"]
    return out


def _invert(price: float | None, F: float, K: float, T: float, r: float,
            kind: str) -> float | None:
    """IV for one leg, or None when the close carries no vol information.

    Two distinct non-answers both become None:

    * A close above the no-arbitrage bound raises out of the solver. That is
      a crossed or stale print, not a solver failure, so it is not retried
      with a fudged input.
    * A close *at* the intrinsic floor inverts to exactly 0.0. At the money
      the floor is near zero, so an absent or stale ATM print lands there and
      the solver dutifully reports zero vol. Writing that down would assert
      the market implied no volatility, which is never true of an ATM option
      and would drag any downstream average toward zero. Zero is the boundary
      the solver returns when the price says nothing, so it is treated as
      nothing -- no threshold required.
    """
    if price is None or price <= 0:
        return None
    try:
        vol = float(implied_vol(price, F, K, T, r, kind, F=F))
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return vol if vol > 0.0 else None


def build_rows(
    bars: list[dict[str, Any]],
    d: date,
    roots: tuple[str, ...] = OPTION_ROOTS,
    data_root: Path | str | None = None,
    rate_fn: Any = None,
) -> list[dict[str, Any]]:
    """ATM term-structure records for one session; pure, so it is testable.

    ``rate_fn(as_of, T) -> float`` is injectable so tests need no rates
    warehouse; it defaults to the landed Treasury curve.
    """
    if rate_fn is None:
        def rate_fn(as_of: date, T: float) -> float:  # noqa: ANN001
            return rate_for(as_of, T, data_root)

    rows: list[dict[str, Any]] = []
    for root in roots:
        chain = _bars_to_chain(bars, root)
        if not chain:
            continue

        def _rate_for_expiry(expiry: date, _root: str = root) -> float:
            return rate_fn(d, max((expiry - d).days, 0) / DAYS_PER_YEAR)

        forwards = forward_from_parity(chain, _rate_for_expiry, asof_date=d)
        legs = _legs_by_expiry(chain)

        for fwd in forwards:
            expiry = date.fromisoformat(str(fwd["expiration_date"])[:10])
            dte = (expiry - d).days
            # A same-day expiry has T=0: no vol reproduces its price, and the
            # inversion would raise. The forward is still meaningful, but a
            # term-structure row without an IV is not, so it is skipped.
            if dte <= 0:
                continue
            T = dte / DAYS_PER_YEAR
            F = float(fwd["forward"])
            r = _rate_for_expiry(expiry)

            strikes = legs.get(fwd["expiration_date"], {})
            if not strikes:
                continue
            K = min(strikes, key=lambda k: abs(k - F))
            call_px = strikes[K].get("call")
            put_px = strikes[K].get("put")

            call_iv = _invert(call_px, F, K, T, r, "call")
            put_iv = _invert(put_px, F, K, T, r, "put")
            got = [v for v in (call_iv, put_iv) if v is not None]

            rows.append({
                "date": d.isoformat(),
                "underlying": root,
                "expiration_date": fwd["expiration_date"],
                "dte": dte,
                "t_years": T,
                "forward": F,
                "atm_strike": K,
                "call_price": call_px,
                "put_price": put_px,
                "call_iv": call_iv,
                "put_iv": put_iv,
                "atm_iv": (sum(got) / len(got)) if got else None,
                "rate": r,
                "pairs": int(fwd.get("pairs") or 0),
                "method": str(fwd.get("method") or ""),
                "src": SRC,
            })
    rows.sort(key=lambda x: (x["underlying"], x["expiration_date"]))
    return rows


def read_day_bars(settings: Settings, d: date) -> list[dict[str, Any]]:
    """Every day-bar row for one session, across all files in the partition."""
    import pyarrow.parquet as pq

    part = Path(settings.data_root) / "clean" / "option_day_bars" / f"dt={d.isoformat()}"
    if not part.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(part.glob("*.parquet")):
        rows.extend(
            pq.read_table(path, columns=["ticker", "close", "window_end_ns"]).to_pylist()
        )
    return rows


def build_for_date(
    settings: Settings, d: date, roots: tuple[str, ...] = OPTION_ROOTS,
) -> list[dict[str, Any]]:
    """Read the partition and reduce it to term-structure rows.

    The curve is loaded once here rather than per expiry. ``load_curve`` has
    to scan every rates partition (the ``dt=`` there is the ingestion run
    date, not the curve date), and a chain has ~100 expiries, so resolving it
    inside the loop re-read the whole rates warehouse a hundred times per
    session. It is a function of the as-of date alone, so hoisting it changes
    no result -- it took a backfill of the full history from ~63 minutes to
    a few.
    """
    curve = load_curve(d, settings.data_root)
    return build_rows(
        read_day_bars(settings, d), d, roots, settings.data_root,
        rate_fn=lambda _as_of, T: curve.at(T),
    )


def main(argv: list[str] | None = None) -> int:
    """Standalone CLI: this is a derivation, not a captured dataset.

    It deliberately does not use ``cli.run_job``. That wrapper gates on the
    market being open and pings a Healthchecks monitor, both of which are
    right for a capture job and wrong for a reduction that is most often run
    in bulk over closed historical days.
    """
    parser = argparse.ArgumentParser(prog="term_structure")
    parser.add_argument("--date", required=True, help="session date, YYYY-MM-DD")
    parser.add_argument("--root", action="append", default=None,
                        help="OPRA root; repeatable (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="compute, write nothing")
    args = parser.parse_args(argv)

    d = date.fromisoformat(args.date)
    roots = tuple(args.root) if args.root else OPTION_ROOTS
    settings = Settings.load()

    try:
        rows = build_for_date(settings, d, roots)
    except RateCurveError as exc:
        print(f"FAIL  rates  {exc}", file=sys.stderr)
        return 1

    if not rows:
        print(f"FAIL  no term structure for {d} "
              f"(no option_day_bars, or no expiry quoting both legs)", file=sys.stderr)
        return 1

    priced = [r for r in rows if r["atm_iv"] is not None]
    by_root: dict[str, int] = {}
    for r in rows:
        by_root[r["underlying"]] = by_root.get(r["underlying"], 0) + 1
    print(f"PASS  {len(rows)} expiries ({len(priced)} with an IV)  "
          + "  ".join(f"{k}={v}" for k, v in sorted(by_root.items())), file=sys.stderr)

    if not args.dry_run:
        path = landing.write_clean(DATASET, d, rows, job="term_structure",
                                   data_root=settings.data_root)
        print(f"PASS  wrote {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
