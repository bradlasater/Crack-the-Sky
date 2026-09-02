"""eod_dayaggs_rest: end-of-day 1-day aggregates for option contracts.

Loads the latest ``contracts`` clean partition at or before ``--date`` (with
``--watchlist``: filtered to 7-45 DTE and strikes within +/-15% of the latest
SPY price), then fetches ``/v2/aggs/ticker/{t}/range/1/day/{date}/{date}``
per contract. 404s and empty results are skipped (contract did not trade).
Progress is logged every 500 tickers; clean rows land in ``option_day_bars``
with ``src='rest'``. ``--limit`` caps the ticker count for testing.
"""

from __future__ import annotations

import sys
from typing import Any

import requests

from ingest.common import landing, ratelimit
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.http_client import MassiveClient
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import (
    DAY_MS,
    compute_watchlist,
    latest_contracts,
    run_date_from_args,
    strip_flag,
)

JOB = "eod_dayaggs_rest"
PROGRESS_EVERY = 500


def _day_bar_record(ticker: str, bar: dict[str, Any]) -> dict[str, Any]:
    """Map one REST day-agg bar to an option_day_bars record (ms -> ns)."""
    start_ms = bar.get("t")
    return {
        "ticker": ticker,
        "window_start_ns": start_ms * 1_000_000 if start_ms is not None else None,
        "window_end_ns": (
            (start_ms + DAY_MS) * 1_000_000 if start_ms is not None else None
        ),
        "open": bar.get("o"),
        "high": bar.get("h"),
        "low": bar.get("l"),
        "close": bar.get("c"),
        "volume": bar.get("v"),
        "vwap": bar.get("vw"),
        "transactions": bar.get("n"),
        "src": "rest",
    }


def _fetch_day_bar(
    client: MassiveClient, ticker: str, run_date
) -> list[dict[str, Any]] | None:
    """Day aggs for one contract; None on 404, [] when the day had no bar."""
    try:
        body = client.get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{run_date}/{run_date}",
            params={"adjusted": "true", "sort": "asc", "limit": 50000},
        )
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise
    return body.get("results") or []


def _main_fn(args, settings: Settings, logger: JsonlLogger, watchlist: bool):
    run_date = run_date_from_args(args)
    if watchlist:
        contracts = compute_watchlist(settings, run_date, logger=logger)
    else:
        contracts = latest_contracts(settings, run_date)
        if not contracts:
            raise RuntimeError(
                "no clean 'contracts' partition found at or before "
                f"{run_date}; run contracts_sync first"
            )
        logger.log("contracts_loaded", run_date=run_date.isoformat(), rows=len(contracts))
    tickers = sorted({c["ticker"] for c in contracts if c.get("ticker")})
    if args.limit is not None:
        tickers = tickers[: args.limit]

    client = MassiveClient(settings, priority=ratelimit.LOW)
    records: list[dict[str, Any]] = []
    raw_bars: list[dict[str, Any]] = []
    skipped_404 = empty = 0
    for idx, ticker in enumerate(tickers, start=1):
        bars = _fetch_day_bar(client, ticker, run_date)
        if bars is None:
            skipped_404 += 1
        elif not bars:
            empty += 1
        else:
            raw_bars.extend({"ticker": ticker, **b} for b in bars)
            records.extend(_day_bar_record(ticker, b) for b in bars)
        if idx % PROGRESS_EVERY == 0 or idx == len(tickers):
            logger.log(
                "progress",
                done=idx,
                total=len(tickers),
                rows=len(records),
                skipped_404=skipped_404,
                empty=empty,
            )
    if not args.dry_run and records:
        raw_path = landing.write_raw("option_day_bars", run_date, raw_bars, job=JOB)
        clean_path = landing.write_clean("option_day_bars", run_date, records, job=JOB)
        logger.log(
            "dayaggs_written",
            rows=len(records),
            raw_path=str(raw_path),
            clean_path=str(clean_path),
        )
    return {
        "rows": len(records),
        "tickers": len(tickers),
        "with_bars": len(tickers) - skipped_404 - empty,
        "skipped_404": skipped_404,
        "empty": empty,
    }


def main(argv: list[str] | None = None) -> None:
    """Entry point: ``python -m ingest.jobs.eod_dayaggs_rest [--watchlist]``."""
    argv, watchlist = strip_flag(list(sys.argv[1:] if argv is None else argv), "--watchlist")

    def main_fn(a, s, log):
        return _main_fn(a, s, log, watchlist)

    run_job(JOB, main_fn, argv)


if __name__ == "__main__":
    main()
