"""grouped_daily: whole-market daily OHLCV in a single REST call.

``/v2/aggs/grouped/locale/us/market/stocks/{date}`` returns every US equity
and ETF for one session in one request (12,518 tickers on 2026-08-28), and it
is entitled on this tier. That makes it the cheapest independent record of
where SPY closed -- independent of the option chain, and independent of
``underlying_bars``, which reconstructs the day from minute aggregates.

Only ``TICKERS`` are kept by default; pass ``--underlying`` to widen, or
``--all`` to land the whole market (still a single API call).

Same-day equity aggregates are NOT entitled (403 "plan doesn't include this
data timeframe"), so this job targets the previous trading day.
"""

from __future__ import annotations

import sys
from typing import Any

from ingest.common import landing, market_gate, ratelimit
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.http_client import MassiveClient
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import parse_underlyings, run_date_from_args, strip_flag

JOB = "grouped_daily"
DATASET = "underlying_day_bars"
GROUPED_PATH = "/v2/aggs/grouped/locale/us/market/stocks"
# One REST call returns the whole market, so widening this list is free.
# VIXY/UVXY/VXX are an independent sanity check on the VIX parity curve in
# clean/forwards -- they are ETF/ETN proxies, not the index, and will drift
# from it by roll cost; that drift is the signal, not an error.
TICKERS = ["SPY", "VIXY", "UVXY", "VXX"]


def _bar_record(bar: dict[str, Any]) -> dict[str, Any]:
    """Map one grouped-daily result to an underlying_day_bars record."""
    return {
        "ticker": bar.get("T"),
        "start_ms": bar.get("t"),
        "open": bar.get("o"),
        "high": bar.get("h"),
        "low": bar.get("l"),
        "close": bar.get("c"),
        "volume": bar.get("v"),
        "vwap": bar.get("vw"),
        "transactions": bar.get("n"),
    }


def _main_fn(args, settings: Settings, logger: JsonlLogger, keep_all: bool):
    run_date = run_date_from_args(args)
    client = MassiveClient(settings, priority=ratelimit.LOW)
    body = client.get(
        f"{GROUPED_PATH}/{run_date.isoformat()}", params={"adjusted": "true"}
    )
    results = body.get("results") or []

    wanted = set(parse_underlyings(args.underlying, TICKERS))
    records = [
        _bar_record(b) for b in results
        if keep_all or str(b.get("T") or "") in wanted
    ]
    if args.limit is not None:
        records = records[: args.limit]

    logger.log(
        "grouped_loaded",
        run_date=run_date.isoformat(),
        market_rows=len(results),
        kept=len(records),
        tickers=sorted(wanted) if not keep_all else "all",
    )
    if not records:
        # An empty result on a trading day means the date was wrong or the
        # market was closed; surface it rather than writing an empty file.
        logger.log("grouped_empty", run_date=run_date.isoformat())
        return {"rows": 0}

    if not args.dry_run:
        raw_path = landing.write_raw(
            DATASET, run_date, records, job=JOB, data_root=settings.data_root
        )
        clean_path = landing.write_clean(
            DATASET, run_date, records, job=JOB, data_root=settings.data_root
        )
        logger.log("grouped_written", rows=len(records),
                   raw_path=str(raw_path), clean_path=str(clean_path))
    return {"rows": len(records)}


def main(argv: list[str] | None = None) -> None:
    """Entry point; defaults --date to the previous trading day."""
    argv, keep_all = strip_flag(list(sys.argv[1:] if argv is None else argv), "--all")
    # "--date=X" is a single argv token; see flatfile_pull.main.
    if not any(a == "--date" or a.startswith("--date=") for a in argv):
        prev = market_gate.previous_trading_day(market_gate.today_et())
        argv += ["--date", prev.isoformat()]

    def main_fn(a, s, log):
        return _main_fn(a, s, log, keep_all)

    run_job(JOB, main_fn, argv)


if __name__ == "__main__":
    main()
