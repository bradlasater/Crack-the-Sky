"""eod_dayaggs_rest: end-of-day 1-day aggregates for option contracts.

Loads the latest ``contracts`` clean partition at or before ``--date`` (with
``--watchlist``: filtered to 7-45 DTE and strikes within +/-15% of the latest
SPY price), then fetches ``/v2/aggs/ticker/{t}/range/1/day/{date}/{date}``
per contract. 404s and empty results are skipped (contract did not trade).
Progress is logged every 500 tickers; clean rows land in ``option_day_bars``
with ``src='rest'``. ``--limit`` caps the ticker count for testing.

The full-universe sweep is one sequential REST call per contract (~100k
contracts ≈ 6 h), so it checkpoints: done tickers and counters go to
``_meta/dayaggs_checkpoint.json`` and fetched raw bars are appended to
``_meta/dayaggs_partial.jsonl`` every ``PROGRESS_EVERY`` contracts. A
restarted run for the same date resumes where the last one stopped instead
of starting over; both files are removed on completion. The watchlist sweep
is small enough to redo and does not checkpoint.
"""

from __future__ import annotations

import json
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
CHECKPOINT_NAME = "dayaggs_checkpoint.json"
PARTIAL_NAME = "dayaggs_partial.jsonl"


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
        # MassiveHTTPError carries ``status_code`` but never sets ``response``,
        # so a response-only check never matches and the first 404 killed the
        # whole sweep instead of skipping the contract that did not trade.
        status = getattr(exc, "status_code", None)
        if status is None and exc.response is not None:
            status = exc.response.status_code
        if status == 404:
            return None
        raise
    return body.get("results") or []


# ---------------------------------------------------------------------------
# Checkpointing (full-universe sweep only)
# ---------------------------------------------------------------------------

def _load_checkpoint(
    settings: Settings, run_date
) -> tuple[set[str], int, int, list[dict[str, Any]]]:
    """Resume state: ``(done_tickers, skipped_404, empty, raw_bars)``.

    Anything missing, corrupt, or written for a different date yields empty
    state. That is deliberate: the failure mode of this file must always be
    "start over" -- refetching a contract is idempotent, while wrongly
    skipping one is a silent gap. Partial bars whose ticker is not in ``done``
    are dropped: a crash can land between appending the bars and saving the
    checkpoint, and refetching those tickers would otherwise double-count
    them.
    """
    path = landing.meta_path(CHECKPOINT_NAME, data_root=settings.data_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return set(), 0, 0, []
    if not isinstance(data, dict) or data.get("run_date") != run_date.isoformat():
        return set(), 0, 0, []
    done_raw = data.get("done")
    if not isinstance(done_raw, list):
        return set(), 0, 0, []
    done = {str(t) for t in done_raw}

    def _counter(name: str) -> int:
        v = data.get(name)
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    bars: list[dict[str, Any]] = []
    partial = landing.meta_path(PARTIAL_NAME, data_root=settings.data_root)
    try:
        for line in partial.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            bar = json.loads(line)
            if isinstance(bar, dict) and str(bar.get("ticker")) in done:
                bars.append(bar)
    except (FileNotFoundError, json.JSONDecodeError):
        return set(), 0, 0, []
    return done, _counter("skipped_404"), _counter("empty"), bars


def _save_checkpoint(
    settings: Settings, run_date, done: set[str], skipped_404: int, empty: int
) -> None:
    """Persist the sweep position atomically-ish (write temp, replace)."""
    path = landing.meta_path(CHECKPOINT_NAME, data_root=settings.data_root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "run_date": run_date.isoformat(),
                "done": sorted(done),
                "skipped_404": skipped_404,
                "empty": empty,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _append_partial_bars(settings: Settings, bars: list[dict[str, Any]]) -> None:
    """Append freshly fetched raw bars to the partial-bars sidecar."""
    if not bars:
        return
    path = landing.meta_path(PARTIAL_NAME, data_root=settings.data_root)
    with open(path, "a", encoding="utf-8") as fh:
        for bar in bars:
            fh.write(json.dumps(bar, default=str) + "\n")


def _clear_checkpoint(settings: Settings) -> None:
    """Remove the checkpoint and partial-bars files after a completed sweep."""
    for name in (CHECKPOINT_NAME, PARTIAL_NAME):
        try:
            landing.meta_path(name, data_root=settings.data_root).unlink()
        except FileNotFoundError:
            pass


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
    # A dry run must not touch real state; the watchlist sweep is small
    # enough to redo from zero.
    checkpointing = not watchlist and not args.dry_run
    done: set[str] = set()
    resumed = 0
    if checkpointing:
        done, skipped_404, empty, prior_bars = _load_checkpoint(settings, run_date)
        resumed = len(done & set(tickers))
        if done:
            raw_bars.extend(prior_bars)
            records.extend(_day_bar_record(str(b["ticker"]), b) for b in prior_bars)
            logger.log(
                "dayaggs_resumed",
                run_date=run_date.isoformat(),
                done=len(done),
                bars=len(prior_bars),
            )
    pending_bars: list[dict[str, Any]] = []
    for idx, ticker in enumerate(tickers, start=1):
        if ticker not in done:
            bars = _fetch_day_bar(client, ticker, run_date)
            if bars is None:
                skipped_404 += 1
            elif not bars:
                empty += 1
            else:
                new_bars = [{"ticker": ticker, **b} for b in bars]
                raw_bars.extend(new_bars)
                pending_bars.extend(new_bars)
                records.extend(_day_bar_record(ticker, b) for b in bars)
            done.add(ticker)
        if idx % PROGRESS_EVERY == 0 or idx == len(tickers):
            logger.log(
                "progress",
                done=idx,
                total=len(tickers),
                rows=len(records),
                skipped_404=skipped_404,
                empty=empty,
            )
            if checkpointing:
                _append_partial_bars(settings, pending_bars)
                pending_bars.clear()
                _save_checkpoint(settings, run_date, done, skipped_404, empty)
    if not args.dry_run and records:
        raw_path = landing.write_raw("option_day_bars", run_date, raw_bars, job=JOB,
                                     data_root=settings.data_root)
        clean_path = landing.write_clean("option_day_bars", run_date, records, job=JOB,
                                         data_root=settings.data_root)
        logger.log(
            "dayaggs_written",
            rows=len(records),
            raw_path=str(raw_path),
            clean_path=str(clean_path),
        )
    if checkpointing:
        _clear_checkpoint(settings)
    return {
        "rows": len(records),
        "tickers": len(tickers),
        "with_bars": len(tickers) - skipped_404 - empty,
        "skipped_404": skipped_404,
        "empty": empty,
        "resumed": resumed,
    }


def main(argv: list[str] | None = None) -> None:
    """Entry point: ``python -m ingest.jobs.eod_dayaggs_rest [--watchlist]``."""
    argv, watchlist = strip_flag(list(sys.argv[1:] if argv is None else argv), "--watchlist")

    def main_fn(a, s, log):
        return _main_fn(a, s, log, watchlist)

    run_job(JOB, main_fn, argv)


if __name__ == "__main__":
    main()
