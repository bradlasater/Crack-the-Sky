"""trades_watchlist: incremental option-trades poll for the watchlist.

The watchlist is the per-underlying 7-45 DTE / +/-15% moneyness / liquid
contract set (see ``ingest.jobs.compute_watchlist``). Cursor state at
``_meta/trades_cursor.json`` maps ``{ticker: last_sip_ts_ns}``; each run
polls ``/v3/trades/{ticker}?timestamp.gte={cursor+1}&sort=timestamp&order=asc``
and paginates fully (hot contracts do ~1,900 trades/min), then updates the
cursors and lands clean ``option_trades`` rows with ``src='rest'``, each under
the ``dt=`` of the day the trade actually happened rather than the day it was
fetched -- an uncursored first poll returns a contract's whole history, so
those are routinely not the same day.

Concurrency: the corrected watchlist is ~8,000 tickers (it was ~2,660 while
SPX was being excluded by a SPY-derived strike band), which is ~26 minutes
serially and cannot be met by any sane schedule -- ``flock -n`` would simply
skip most runs, which is precisely the kind of silent shortfall this job is
supposed to avoid. Tickers are therefore polled through a thread pool whose
total outbound rate is bounded by the shared token bucket in
``ingest.common.ratelimit``, not by the pool size.

Adaptive polling: the watchlist is ~9,100 tickers but a median five-minute
slot only has trades in ~850 of them (measured 2026-09-03), so ~90% of every
run's requests returned nothing while the job sat pinned against the shared
40 rps bucket -- 248s of a 300s slot, with six overruns and nine skipped
slots that day. Tickers that keep coming back empty are therefore polled less
often, on the ladder in :data:`BACKOFF_MAX` below.

The safety property that makes this cheap: skipping a poll does not touch the
ticker's cursor, so the next poll still asks for everything since the last
trade actually seen. Backoff can only ever delay a trade's *arrival*, never
drop it -- and ``_meta/trades_poll_state.json`` is a pure optimisation hint
that can be deleted at any time, costing one expensive run and nothing else.
The cursor file remains the only correctness-critical state.

This job is a same-day convenience. The ``trades_v1`` flat file pulled the
next morning is the authoritative record and is strictly more complete.
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from ingest.common import landing, market_gate, ratelimit
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.http_client import MassiveClient
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import compute_watchlist, run_date_from_args

JOB = "trades_watchlist"
TRADES_PATH = "/v3/trades"
CURSOR_NAME = "trades_cursor.json"
POLL_STATE_NAME = "trades_poll_state.json"
DEFAULT_CONCURRENCY = int(os.environ.get("TRADES_CONCURRENCY", "8"))

# Longest gap, in five-minute slots, between polls of a persistently silent
# ticker. The ladder is linear up to this cap: a ticker that has come back
# empty ``n`` times in a row is polled every ``min(n, BACKOFF_MAX)`` slots, so
# one quiet slot costs nothing and only a sustained silence earns a real skip.
#
# 4 slots = 20 minutes of same-day latency on the least active contracts,
# against a ~3x cut in requests. Set TRADES_BACKOFF_MAX=1 to disable entirely
# (every ticker every slot, the pre-2026-09 behaviour).
BACKOFF_MAX = max(1, int(os.environ.get("TRADES_BACKOFF_MAX", "4")))


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
    """Read ``_meta/trades_cursor.json`` (empty dict when missing/corrupt).

    "Corrupt" includes valid JSON of the wrong shape: a list, or values that
    are not integer timestamps. Loading is the first thing the job does, so an
    exception here fails every run until the file is removed by hand -- bad
    entries are dropped instead, and the affected tickers simply re-poll from
    scratch (duplicates, never gaps: the cursor only ever moves forward).
    """
    path = landing.meta_path(CURSOR_NAME, data_root=settings.data_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cursors: dict[str, int] = {}
    for k, v in data.items():
        # Keep only real JSON integers: bools are ints in Python, and a huge
        # exponent like 1e1000 parses to inf (int(inf) raises OverflowError,
        # which would brick every run) -- drop all of those.
        if isinstance(v, bool) or not isinstance(v, int):
            continue
        cursors[str(k)] = v
    return cursors


def _save_cursors(settings: Settings, cursors: dict[str, int]) -> None:
    """Persist the cursor state atomically-ish (write temp, replace)."""
    path = landing.meta_path(CURSOR_NAME, data_root=settings.data_root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cursors, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _load_poll_state(settings: Settings, run_date: date) -> tuple[int, dict[str, list[int]]]:
    """Read ``_meta/trades_poll_state.json`` as ``(run_index, {ticker: [silent, due]})``.

    Anything unreadable, or written for a different session, yields empty
    state. That is deliberate: the failure mode of this file must always be
    "poll everything", never "skip something". A fresh session in particular
    must not inherit yesterday's silence counters -- the open is exactly when
    the whole book goes live again and precisely the wrong moment to be
    skipping three quarters of it.
    """
    path = landing.meta_path(POLL_STATE_NAME, data_root=settings.data_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("date") != run_date.isoformat():
            return 0, {}
        tickers = {
            str(k): [int(v[0]), int(v[1])] for k, v in dict(data["tickers"]).items()
        }
        return int(data["run"]), tickers
    except Exception:  # noqa: BLE001 - see below
        # Deliberately broad. The contract this file has with the job is
        # "never cause a skip", and there are more ways to be malformed than
        # to be well-formed: a top-level list raises AttributeError on .get,
        # a truncated ticker value raises IndexError on v[1], a non-numeric
        # one raises ValueError. Enumerating them invites the next shape to
        # crash the job over an optimisation hint.
        return 0, {}


def _save_poll_state(
    settings: Settings, run_date: date, run_index: int, state: dict[str, list[int]]
) -> None:
    """Persist the backoff bookkeeping (write temp, replace)."""
    path = landing.meta_path(POLL_STATE_NAME, data_root=settings.data_root)
    tmp = path.with_suffix(".tmp")
    payload = {"date": run_date.isoformat(), "run": run_index, "tickers": state}
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


# The last trades slot of the day is "0-30/5 16", so any run at or after this
# is the closing one. It ignores backoff and sweeps every ticker.
#
# Without this, a quiet ticker could fall due after the final run and have its
# closing trades left for the next morning. Those trades are no longer
# misfiled when that happens -- _by_trade_date sends them to the session they
# belong to -- but they would still arrive a day late, and the same-day tape
# is the entire point of this job. The sweep is about latency now, not
# partitioning.
#
# Cost is one full sweep a day -- ~9,100 requests, ~150s at the current rate.
CLOSING_SWEEP_AFTER_ET = time(16, 25)


def _now_et() -> datetime:
    """Indirection so tests can pin the clock (see _is_closing_run)."""
    return market_gate.now_et()


def _is_closing_run(now: datetime) -> bool:
    """True for the day's final trades slot, which never backs off."""
    return now.time() >= CLOSING_SWEEP_AFTER_ET


def _due_tickers(
    tickers: list[str],
    run_index: int,
    state: dict[str, list[int]],
    closing: bool = False,
) -> list[str]:
    """Tickers to poll this run: everything unknown, plus everything due.

    A ticker absent from ``state`` has never been seen -- a new contract in
    the watchlist, or a wiped state file -- and is always polled. On the
    closing run every ticker is due, whatever the ladder says.
    """
    if closing:
        return list(tickers)
    return [t for t in tickers if t not in state or state[t][1] <= run_index]


def _next_due(silent: int, run_index: int) -> list[int]:
    """``[silent, due]`` after a poll that saw ``silent`` empties in a row."""
    return [silent, run_index + min(max(silent, 1), BACKOFF_MAX)]


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
    # raw and records are built in lockstep and stay index-aligned --
    # _by_trade_date zips them to split a run across trade-date partitions.
    for trade in client.paginate(f"{TRADES_PATH}/{ticker}", params=params, limit=1000):
        raw.append({"ticker": ticker, **trade})
        records.append(_trade_record(ticker, trade))
        ts = trade.get("sip_timestamp")
        if ts is not None and (max_ts is None or ts > max_ts):
            max_ts = ts
    return ticker, raw, records, max_ts


def trade_date(sip_timestamp_ns: int | None, fallback: date) -> date:
    """ET calendar date a trade happened on; ``fallback`` when it has no stamp.

    Integer seconds, not ``ns / 1e9``: a float cannot hold nanosecond epochs
    exactly, and a date derived from a rounded instant is a date that can be
    wrong at midnight.
    """
    if sip_timestamp_ns is None:
        return fallback
    return datetime.fromtimestamp(
        sip_timestamp_ns // 1_000_000_000, market_gate.ET
    ).date()


def _by_trade_date(
    records: list[dict[str, Any]], raw: list[dict[str, Any]], run_date: date
) -> dict[date, tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    """Split one run's output into ``{trade_date: (raw_rows, clean_rows)}``.

    dt= must mean "the day these trades happened", not "the day we happened to
    fetch them". Those coincide for a warm cursor and diverge badly without
    one: a contract's first-ever poll has no cursor and returns its entire
    history, which is how 1,594,219 rows of older trades (87.6% of the
    partition's REST rows) came to sit under dt=2026-09-01.
    """
    out: dict[date, tuple[list, list]] = {}
    # strict: the lockstep between raw and records is a contract, not a hope.
    for rec, raw_rec in zip(records, raw, strict=True):
        d = trade_date(rec.get("sip_timestamp_ns"), run_date)
        bucket = out.setdefault(d, ([], []))
        bucket[0].append(raw_rec)
        bucket[1].append(rec)
    return out


# The flat-file dataset behind clean/option_trades. A manifest entry for this
# dataset+date is flatfile_pull's completion record.
FLATFILE_DATASET = "trades_v1"


def _completed_flatfile_dates(settings: Settings) -> set[str]:
    """Dates ``flatfile_pull`` has *finished* landing, per its own manifest."""
    from ingest.jobs.flatfile_pull import manifest_dates

    return manifest_dates(Path(settings.data_root), FLATFILE_DATASET)


def _flatfile_covered(settings: Settings, day: date, completed: set[str]) -> bool:
    """Has ``flatfile_pull`` already landed the authoritative record for ``day``?

    Both halves are required, and neither is sufficient.

    ``landing.write_clean`` writes straight to the final ``.parquet`` path --
    no temp-then-rename -- and ``flatfile_pull`` appends its manifest entry
    only afterwards. The two jobs also overlap: flatfile_pull runs at 11:05
    and this one runs every five minutes straight through it. So a bare glob
    can match a half-written or failed-mid-write file, and dropping REST rows
    on the strength of that -- then persisting cursors past them -- would
    leave no usable copy of those trades anywhere.

    The manifest entry is the completion record, so require it too. Requiring
    only the manifest would be wrong in the other direction: an entry with the
    parquet since pruned is not coverage either.
    """
    if day.isoformat() not in completed:
        return False
    part = Path(settings.data_root) / "clean" / "option_trades" / f"dt={day.isoformat()}"
    return any(part.glob("flatfile_pull-*.parquet"))


def _main_fn(args, settings: Settings, logger: JsonlLogger):
    run_date = run_date_from_args(args)
    watchlist = compute_watchlist(settings, run_date, logger=logger)
    tickers = sorted({c["ticker"] for c in watchlist if c.get("ticker")})
    if args.limit is not None:
        tickers = tickers[: args.limit]

    cursors = _load_cursors(settings)
    prev_run, poll_state = _load_poll_state(settings, run_date)
    run_index = prev_run + 1
    closing = _is_closing_run(_now_et())
    due = _due_tickers(tickers, run_index, poll_state, closing=closing)

    client = MassiveClient(settings, priority=ratelimit.LOW)
    records: list[dict[str, Any]] = []
    raw_trades: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    merge_lock = threading.Lock()
    workers = max(1, min(DEFAULT_CONCURRENCY, len(due))) if due else 1

    def work(ticker: str) -> None:
        try:
            name, raw, recs, max_ts = _poll_ticker(client, ticker, cursors.get(ticker))
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not kill the run
            with merge_lock:
                errors.append({"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"})
                # A failed poll is not evidence of silence. Retry next slot.
                poll_state[ticker] = [0, run_index + 1]
            return
        # Cursors, the record buffers and the backoff state are shared; merge
        # under the lock or concurrent writers corrupt _meta/trades_cursor.json.
        with merge_lock:
            raw_trades.extend(raw)
            records.extend(recs)
            if max_ts is not None:
                cursors[name] = max_ts
            silent = 0 if recs else poll_state.get(name, [0, 0])[0] + 1
            poll_state[name] = _next_due(silent, run_index)

    logger.log(
        "poll_start",
        tickers=len(tickers),
        polled=len(due),
        skipped=len(tickers) - len(due),
        backoff_max=BACKOFF_MAX,
        closing_sweep=closing,
        workers=workers,
    )
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=JOB) as pool:
        list(pool.map(work, due))

    # Contracts that have rolled off the watchlist must not accumulate in the
    # state file forever. Skipped entirely under --limit: `tickers` was
    # truncated above, so pruning against it would delete the backoff state
    # for every contract the limited run never looked at, and the next real
    # run would poll all of them at once.
    if args.limit is None:
        keep = set(tickers)
        poll_state = {t: v for t, v in poll_state.items() if t in keep}

    for err in errors[:20]:
        logger.log("ticker_error", **err)
    logger.log(
        "poll_done",
        tickers=len(tickers),
        polled=len(due),
        rows=len(records),
        errors=len(errors),
    )

    # A quiet tape yields rows=0 and is fine; a run where EVERY ticker errored
    # is an outage (lost entitlement, broken endpoint), and must not report
    # success. One bad ticker still must not kill the run -- only 100%.
    if tickers and len(errors) == len(tickers):
        raise RuntimeError(
            f"every ticker poll failed ({len(errors)}/{len(tickers)}); "
            f"first: {errors[0]['error']}"
        )

    if not args.dry_run:
        if records:
            partitions = _by_trade_date(records, raw_trades, run_date)
            # An uncursored first poll drags in a contract's whole history --
            # 2,544 rows across 165 past sessions on 2026-09-03 alone. Filing
            # those by trade date is correct but pointless: flatfile_pull has
            # already landed the authoritative, strictly more complete record
            # for those days, so each would add a one- or two-row parquet to a
            # partition that was already right. Drop them, and say how many.
            completed = _completed_flatfile_dates(settings)
            covered = {
                d for d in partitions
                if d != run_date and _flatfile_covered(settings, d, completed)
            }
            dropped = sum(len(partitions[d][1]) for d in covered)
            if covered:
                logger.log(
                    "trades_dropped_covered",
                    rows=dropped,
                    days=len(covered),
                    first=min(covered).isoformat(),
                    last=max(covered).isoformat(),
                    reason="flat file already holds these sessions",
                )
            for day in sorted(set(partitions) - covered):
                raw_rows, clean_rows = partitions[day]
                raw_path = landing.write_raw(
                    "option_trades", day, raw_rows, job=JOB,
                    data_root=settings.data_root,
                )
                clean_path = landing.write_clean(
                    "option_trades", day, clean_rows, job=JOB,
                    data_root=settings.data_root,
                )
                logger.log(
                    "trades_written",
                    dt=day.isoformat(),
                    rows=len(clean_rows),
                    backfilled=day != run_date,
                    raw_path=str(raw_path),
                    clean_path=str(clean_path),
                )
            today_rows = len(partitions.get(run_date, ([], []))[1])
            logger.log(
                "trades_partitioned",
                rows=len(records),
                written=len(records) - dropped,
                partitions=len(partitions) - len(covered),
                today=today_rows,
                other_days=len(records) - today_rows - dropped,
            )
        _save_cursors(settings, cursors)
        _save_poll_state(settings, run_date, run_index, poll_state)
        logger.log("cursors_saved", tickers=len(cursors))
    return {
        "rows": len(records),
        "tickers": len(tickers),
        "polled": len(due),
        "skipped": len(tickers) - len(due),
        "errors": len(errors),
    }


def main(argv: list[str] | None = None) -> None:
    """Entry point: ``python -m ingest.jobs.trades_watchlist``."""
    run_job(JOB, _main_fn, argv)


if __name__ == "__main__":
    main()
