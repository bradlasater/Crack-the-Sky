"""trades_watchlist: incremental option-trades poll for the watchlist.

The watchlist is the per-underlying 7-45 DTE / +/-15% moneyness / liquid
contract set (see ``ingest.jobs.compute_watchlist``). Cursor state at
``_meta/trades_cursor.json`` maps ``{ticker: last_sip_ts_ns}``; each run
polls ``/v3/trades/{ticker}?timestamp.gte={cursor+1}&sort=timestamp&order=asc``
and paginates fully (hot contracts do ~1,900 trades/min), then updates the
cursors and lands clean ``option_trades`` rows with ``src='rest'``.

Concurrency: the corrected watchlist is ~8,000 tickers (it was ~2,660 while
SPX was being excluded by a SPY-derived strike band), which is ~26 minutes
serially and cannot be met by any sane schedule -- ``flock -n`` would simply
skip most runs, which is precisely the kind of silent shortfall this job is
supposed to avoid. Tickers are therefore polled through a thread pool whose
total outbound rate is bounded by the shared token bucket in
``ingest.common.ratelimit``, not by the pool size.

This job is a same-day convenience. The ``trades_v1`` flat file pulled the
next morning is the authoritative record and is strictly more complete.
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ingest.common import landing, ratelimit
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.http_client import MassiveClient
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import compute_watchlist, run_date_from_args

JOB = "trades_watchlist"
TRADES_PATH = "/v3/trades"
CURSOR_NAME = "trades_cursor.json"
DEFAULT_CONCURRENCY = int(os.environ.get("TRADES_CONCURRENCY", "8"))


def _trade_record(ticker: str, trade: dict[str, Any]) -> dict[str, Any]:
    """Map one ``/v3/trades`` result to an option_trades record."""
    conditions = trade.get("conditions")
    return {
        "ticker": ticker,
        "price": trade.get("price"),
        "size": trade.get("size"),
        "exchange": trade.get("exchange"),
        "conditions": json.dumps(conditions) if conditions is not None else None,
        "correction": trade.get("correction"),
        "trade_id": trade.get("id"),
        "sequence_number": trade.get("sequence_number"),
        "sip_timestamp_ns": trade.get("sip_timestamp"),
        "participant_timestamp_ns": trade.get("participant_timestamp"),
        "src": "rest",
    }


def _load_cursors(settings: Settings) -> dict[str, int]:
    """Read ``_meta/trades_cursor.json`` (empty dict when missing/corrupt)."""
    path = landing.meta_path(CURSOR_NAME, data_root=settings.data_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {str(k): int(v) for k, v in data.items()}


def _save_cursors(settings: Settings, cursors: dict[str, int]) -> None:
    """Persist the cursor state atomically-ish (write temp, replace)."""
    path = landing.meta_path(CURSOR_NAME, data_root=settings.data_root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cursors, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _poll_ticker(
    client: MassiveClient, ticker: str, cursor: int | None
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], int | None]:
    """Fetch all trades for one ticker since ``cursor``.

    Pure with respect to shared state -- returns what it found so the caller
    can merge under a lock. Runs on a pool thread.
    """
    params: dict[str, Any] = {"sort": "timestamp", "order": "asc"}
    if cursor is not None:
        params["timestamp.gte"] = cursor + 1
    max_ts = cursor
    raw: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for trade in client.paginate(f"{TRADES_PATH}/{ticker}", params=params, limit=1000):
        raw.append({"ticker": ticker, **trade})
        records.append(_trade_record(ticker, trade))
        ts = trade.get("sip_timestamp")
        if ts is not None and (max_ts is None or ts > max_ts):
            max_ts = ts
    return ticker, raw, records, max_ts


def _main_fn(args, settings: Settings, logger: JsonlLogger):
    run_date = run_date_from_args(args)
    watchlist = compute_watchlist(settings, run_date, logger=logger)
    tickers = sorted({c["ticker"] for c in watchlist if c.get("ticker")})
    if args.limit is not None:
        tickers = tickers[: args.limit]

    cursors = _load_cursors(settings)
    client = MassiveClient(settings, priority=ratelimit.LOW)
    records: list[dict[str, Any]] = []
    raw_trades: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    merge_lock = threading.Lock()
    workers = max(1, min(DEFAULT_CONCURRENCY, len(tickers)))

    def work(ticker: str) -> None:
        try:
            name, raw, recs, max_ts = _poll_ticker(client, ticker, cursors.get(ticker))
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not kill the run
            with merge_lock:
                errors.append({"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"})
            return
        # Cursors and the record buffers are shared; merge under the lock or
        # concurrent writers corrupt _meta/trades_cursor.json.
        with merge_lock:
            raw_trades.extend(raw)
            records.extend(recs)
            if max_ts is not None:
                cursors[name] = max_ts

    logger.log("poll_start", tickers=len(tickers), workers=workers)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=JOB) as pool:
        list(pool.map(work, tickers))

    for err in errors[:20]:
        logger.log("ticker_error", **err)
    logger.log(
        "poll_done",
        tickers=len(tickers),
        rows=len(records),
        errors=len(errors),
    )

    if not args.dry_run:
        if records:
            raw_path = landing.write_raw(
                "option_trades", run_date, raw_trades, job=JOB,
                data_root=settings.data_root,
            )
            clean_path = landing.write_clean(
                "option_trades", run_date, records, job=JOB,
                data_root=settings.data_root,
            )
            logger.log(
                "trades_written",
                rows=len(records),
                raw_path=str(raw_path),
                clean_path=str(clean_path),
            )
        _save_cursors(settings, cursors)
        logger.log("cursors_saved", tickers=len(cursors))
    return {"rows": len(records), "tickers": len(tickers), "errors": len(errors)}


def main(argv: list[str] | None = None) -> None:
    """Entry point: ``python -m ingest.jobs.trades_watchlist``."""
    run_job(JOB, _main_fn, argv)


if __name__ == "__main__":
    main()
