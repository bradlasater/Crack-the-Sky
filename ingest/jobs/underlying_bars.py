"""underlying_bars: SPY 1-minute aggregates for a trading date.

Fetches ``/v2/aggs/ticker/SPY/range/1/minute/{date}/{date}`` (adjusted,
sorted, limit 50000) and lands raw JSONL plus a clean
``underlying_minute_bars`` parquet partition. Bar timestamps are stored
exactly as delivered (``t`` = ms epoch -> ``start_ms``).

NOTE (tier entitlement): same-day SPY minute aggs are 403 on the Options
Developer plan ("plan doesn't include this data timeframe"); T-1 works. The
cron schedule therefore runs this job once each morning with
``--prev-trading-day`` (T-1 session) instead of intraday; the intraday SPY
price comes from ``snapshot_sweep``'s ``underlying_price`` field.
"""

from __future__ import annotations

import sys

from typing import Any

from ingest.common import landing, market_gate
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.http_client import MassiveClient
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import run_date_from_args, strip_flag

JOB = "underlying_bars"
DEFAULT_TICKERS = ["SPY"]


def _bar_record(ticker: str, bar: dict[str, Any]) -> dict[str, Any]:
    """Map one REST aggs bar to an underlying_minute_bars record (ms kept)."""
    return {
        "ticker": ticker,
        "start_ms": bar.get("t"),
        "open": bar.get("o"),
        "high": bar.get("h"),
        "low": bar.get("l"),
        "close": bar.get("c"),
        "volume": bar.get("v"),
        "vwap": bar.get("vw"),
        "transactions": bar.get("n"),
    }


def _main_fn(args, settings: Settings, logger: JsonlLogger):
    run_date = run_date_from_args(args)
    client = MassiveClient(settings)
    totals = {"rows": 0}
    for ticker in DEFAULT_TICKERS:
        body = client.get(
            f"/v2/aggs/ticker/{ticker}/range/1/minute/{run_date}/{run_date}",
            params={"adjusted": "true", "sort": "asc", "limit": 50000},
        )
        raw_bars = body.get("results") or []
        records = [_bar_record(ticker, b) for b in raw_bars]
        if not args.dry_run and records:
            raw_path = landing.write_raw(
                "underlying_minute_bars", run_date,
                ({"ticker": ticker, **b} for b in raw_bars),
                job=f"{JOB}-{ticker}",
            )
            clean_path = landing.write_clean(
                "underlying_minute_bars", run_date, records, job=f"{JOB}-{ticker}"
            )
            logger.log(
                "underlying_bars_synced",
                ticker=ticker,
                rows=len(records),
                raw_path=str(raw_path),
                clean_path=str(clean_path),
            )
        totals["rows"] += len(records)
    return totals


def main(argv: list[str] | None = None) -> None:
    """Entry point: ``python -m ingest.jobs.underlying_bars [--date D | --prev-trading-day]``.

    ``--prev-trading-day`` resolves ``--date`` to the previous trading day
    (via :func:`market_gate.previous_trading_day`) unless ``--date`` was
    given explicitly — same default pattern ``flatfile_pull`` uses, so the
    08:05 Tue–Sat cron run always targets yesterday's session.
    """
    argv, prev = strip_flag(list(sys.argv[1:] if argv is None else argv),
                            "--prev-trading-day")
    if prev and "--date" not in argv:
        argv += ["--date", market_gate.previous_trading_day(market_gate.today_et()).isoformat()]
    run_job(JOB, _main_fn, argv)


if __name__ == "__main__":
    main()
