"""dividends_sync: SPY dividends and splits reference data.

Paginates ``/v3/reference/dividends`` and ``/v3/reference/splits`` for SPY
and lands raw JSONL plus clean ``dividends`` / ``splits`` parquet partitions.
"""

from __future__ import annotations

from itertools import islice
from typing import Any

from ingest.common import landing, ratelimit
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.http_client import MassiveClient
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import parse_underlyings, run_date_from_args

JOB = "dividends_sync"
DEFAULT_TICKERS = ["SPY"]


def _dividend_record(result: dict[str, Any]) -> dict[str, Any]:
    """Map one ``/v3/reference/dividends`` result to a dividends record."""
    return {
        "ticker": result.get("ticker"),
        "dividend_id": result.get("id"),
        "cash_amount": result.get("cash_amount"),
        "currency": result.get("currency"),
        "dividend_type": result.get("dividend_type"),
        "frequency": result.get("frequency"),
        "declaration_date": result.get("declaration_date"),
        "ex_dividend_date": result.get("ex_dividend_date"),
        "record_date": result.get("record_date"),
        "pay_date": result.get("pay_date"),
    }


def _split_record(result: dict[str, Any]) -> dict[str, Any]:
    """Map one ``/v3/reference/splits`` result to a splits record."""
    return {
        "ticker": result.get("ticker"),
        "split_id": result.get("id"),
        "execution_date": result.get("execution_date"),
        "split_from": result.get("split_from"),
        "split_to": result.get("split_to"),
    }


def _sync_dataset(
    client: MassiveClient,
    settings: Settings,
    logger: JsonlLogger,
    args,
    ticker: str,
    dataset: str,
    path: str,
    mapper,
) -> int:
    """Fetch and land one reference dataset for one ticker; returns rows."""
    run_date = run_date_from_args(args)
    stream = client.paginate(path, params={"ticker": ticker, "order": "asc"}, limit=1000)
    if args.limit is not None:
        stream = islice(stream, args.limit)
    raw_results = list(stream)
    records = [mapper(r) for r in raw_results]
    if not args.dry_run:
        raw_path = landing.write_raw(dataset, run_date, raw_results, job=f"{JOB}-{ticker}")
        clean_path = landing.write_clean(dataset, run_date, records, job=f"{JOB}-{ticker}")
        logger.log(
            "reference_synced",
            dataset=dataset,
            ticker=ticker,
            rows=len(records),
            raw_path=str(raw_path),
            clean_path=str(clean_path),
        )
    return len(records)


def _main_fn(args, settings: Settings, logger: JsonlLogger):
    client = MassiveClient(settings, priority=ratelimit.LOW)
    tickers = parse_underlyings(args.underlying, DEFAULT_TICKERS)
    totals = {"rows": 0}
    for ticker in tickers:
        totals["rows"] += _sync_dataset(
            client, settings, logger, args,
            ticker, "dividends", "/v3/reference/dividends", _dividend_record,
        )
        totals["rows"] += _sync_dataset(
            client, settings, logger, args,
            ticker, "splits", "/v3/reference/splits", _split_record,
        )
    return totals


def main(argv: list[str] | None = None) -> None:
    """Entry point: ``python -m ingest.jobs.dividends_sync``."""
    run_job(JOB, _main_fn, argv)


if __name__ == "__main__":
    main()
