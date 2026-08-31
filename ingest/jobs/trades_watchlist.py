"""trades_watchlist: incremental option-trades poll for the watchlist.

The watchlist is the 7-45 DTE, +/-15% moneyness contract set (see
``ingest.jobs.compute_watchlist``). Cursor state at
``_meta/trades_cursor.json`` maps ``{ticker: last_sip_ts_ns}``; each run
polls ``/v3/trades/{ticker}?timestamp.gte={cursor+1}&sort=timestamp&order=asc``
and paginates fully (hot contracts do ~1,900 trades/min), then updates the
cursors and lands clean ``option_trades`` rows with ``src='rest'``.
"""

from __future__ import annotations

import json
from typing import Any

from ingest.common import landing
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.http_client import MassiveClient
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import compute_watchlist, run_date_from_args

JOB = "trades_watchlist"
TRADES_PATH = "/v3/trades"
CURSOR_NAME = "trades_cursor.json"


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
    path = landing.meta_path(CURSOR_NAME)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {str(k): int(v) for k, v in data.items()}


def _save_cursors(settings: Settings, cursors: dict[str, int]) -> None:
    """Persist the cursor state atomically-ish (write temp, replace)."""
    path = landing.meta_path(CURSOR_NAME)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cursors, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _main_fn(args, settings: Settings, logger: JsonlLogger):
    run_date = run_date_from_args(args)
    watchlist = compute_watchlist(settings, run_date, logger=logger)
    tickers = sorted({c["ticker"] for c in watchlist if c.get("ticker")})
    if args.limit is not None:
        tickers = tickers[: args.limit]

    cursors = _load_cursors(settings)
    client = MassiveClient(settings)
    records: list[dict[str, Any]] = []
    raw_trades: list[dict[str, Any]] = []
    for ticker in tickers:
        params: dict[str, Any] = {"sort": "timestamp", "order": "asc"}
        if ticker in cursors:
            params["timestamp.gte"] = cursors[ticker] + 1
        max_ts = cursors.get(ticker)
        n = 0
        for trade in client.paginate(
            f"{TRADES_PATH}/{ticker}", params=params, limit=1000
        ):
            raw_trades.append({"ticker": ticker, **trade})
            records.append(_trade_record(ticker, trade))
            ts = trade.get("sip_timestamp")
            if ts is not None and (max_ts is None or ts > max_ts):
                max_ts = ts
            n += 1
        if max_ts is not None:
            cursors[ticker] = max_ts
        logger.log("ticker_polled", ticker=ticker, trades=n, cursor=max_ts)

    if not args.dry_run:
        if records:
            raw_path = landing.write_raw("option_trades", run_date, raw_trades, job=JOB)
            clean_path = landing.write_clean("option_trades", run_date, records, job=JOB)
            logger.log(
                "trades_written",
                rows=len(records),
                raw_path=str(raw_path),
                clean_path=str(clean_path),
            )
        _save_cursors(settings, cursors)
        logger.log("cursors_saved", tickers=len(cursors))
    return {"rows": len(records), "tickers": len(tickers)}


def main(argv: list[str] | None = None) -> None:
    """Entry point: ``python -m ingest.jobs.trades_watchlist``."""
    run_job(JOB, _main_fn, argv)


if __name__ == "__main__":
    main()
