"""Tests for the concurrent trades poll and its cursor handling.

Cursors are the resume state: if concurrent workers corrupt or lose them, the
next run silently re-fetches or (worse) skips trades. These lock down the
merge semantics without touching the network.
"""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

import pytest

from ingest.common import landing
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import trades_watchlist as job


def _settings(data_root: Path) -> Settings:
    return Settings(
        massive_api_key="test-key",
        data_root=data_root,
        log_root=data_root / "logs",
    )


class _FakeClient:
    """Stands in for MassiveClient.paginate, with a per-ticker trade tape."""

    def __init__(self, tape: dict[str, list[dict]]) -> None:
        self.tape = tape
        self.seen: list[tuple[str, int | None]] = []
        self._lock = threading.Lock()

    def paginate(self, path, params=None, limit=1000):
        ticker = path.rsplit("/", 1)[-1]
        gte = (params or {}).get("timestamp.gte")
        with self._lock:
            self.seen.append((ticker, gte))
        for trade in self.tape.get(ticker, []):
            if gte is None or trade["sip_timestamp"] >= gte:
                yield trade


def _trade(ts: int, price: float = 1.0) -> dict:
    return {"sip_timestamp": ts, "price": price, "size": 1, "exchange": 300,
            "conditions": [209], "correction": 0, "id": str(ts),
            "sequence_number": ts}


# ---------------------------------------------------------------------------
# Single-ticker poll
# ---------------------------------------------------------------------------

def test_poll_ticker_returns_max_timestamp() -> None:
    client = _FakeClient({"O:SPY1": [_trade(10), _trade(30), _trade(20)]})
    name, raw, records, max_ts = job._poll_ticker(client, "O:SPY1", None)
    assert name == "O:SPY1"
    assert len(raw) == len(records) == 3
    assert max_ts == 30
    assert all(r["src"] == "rest" for r in records)


def test_poll_ticker_resumes_past_the_cursor() -> None:
    client = _FakeClient({"O:SPY1": [_trade(10), _trade(20), _trade(30)]})
    _name, _raw, records, max_ts = job._poll_ticker(client, "O:SPY1", 20)
    # cursor+1 is requested, so the trade AT the cursor is not re-fetched.
    assert client.seen == [("O:SPY1", 21)]
    assert [r["sip_timestamp_ns"] for r in records] == [30]
    assert max_ts == 30


def test_poll_ticker_keeps_cursor_when_no_new_trades() -> None:
    client = _FakeClient({"O:SPY1": []})
    _name, _raw, records, max_ts = job._poll_ticker(client, "O:SPY1", 99)
    assert records == []
    assert max_ts == 99, "an empty poll must not rewind the cursor"


def test_trade_record_json_encodes_conditions() -> None:
    rec = job._trade_record("O:SPY1", _trade(10))
    assert json.loads(rec["conditions"]) == [209]


# ---------------------------------------------------------------------------
# Cursor persistence
# ---------------------------------------------------------------------------

def test_cursor_roundtrip_uses_configured_data_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    job._save_cursors(settings, {"O:SPY1": 10, "O:SPX1": 20})
    assert (tmp_path / "_meta" / job.CURSOR_NAME).is_file()
    assert job._load_cursors(settings) == {"O:SPY1": 10, "O:SPX1": 20}


def test_load_cursors_tolerates_corruption(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    landing.meta_path(job.CURSOR_NAME, data_root=tmp_path).write_text(
        "{not json", encoding="utf-8"
    )
    assert job._load_cursors(settings) == {}


def test_load_cursors_tolerates_wrong_json_shape(tmp_path: Path) -> None:
    """Valid JSON that is not an object must not crash the job every run."""
    settings = _settings(tmp_path)
    landing.meta_path(job.CURSOR_NAME, data_root=tmp_path).write_text(
        '["O:SPY1"]', encoding="utf-8"
    )
    assert job._load_cursors(settings) == {}


def test_load_cursors_drops_non_integer_values(tmp_path: Path) -> None:
    """Bad entries are dropped (re-poll, duplicates at worst); good ones keep."""
    settings = _settings(tmp_path)
    landing.meta_path(job.CURSOR_NAME, data_root=tmp_path).write_text(
        json.dumps({"O:SPY1": 10, "O:SPY2": "not-a-ts", "O:SPY3": None}),
        encoding="utf-8",
    )
    assert job._load_cursors(settings) == {"O:SPY1": 10}


def test_load_cursors_drops_floats_and_bools(tmp_path: Path) -> None:
    """A cursor must be a real JSON integer; floats (even integral ones) and
    booleans (ints in Python) are dropped, not coerced."""
    settings = _settings(tmp_path)
    landing.meta_path(job.CURSOR_NAME, data_root=tmp_path).write_text(
        '{"O:SPY1": 10, "O:SPY2": 10.0, "O:SPY3": 1.5, "O:SPY4": true, "O:SPY5": false}',
        encoding="utf-8",
    )
    assert job._load_cursors(settings) == {"O:SPY1": 10}


def test_load_cursors_drops_huge_exponent_without_raising(tmp_path: Path) -> None:
    """1e1000 parses to inf; int(inf) would raise OverflowError and brick
    every run -- the entry must be dropped instead."""
    settings = _settings(tmp_path)
    landing.meta_path(job.CURSOR_NAME, data_root=tmp_path).write_text(
        '{"O:SPY1": 10, "O:SPY2": 1e1000}',
        encoding="utf-8",
    )
    assert job._load_cursors(settings) == {"O:SPY1": 10}


# ---------------------------------------------------------------------------
# Concurrent merge
# ---------------------------------------------------------------------------

def test_concurrent_merge_keeps_every_ticker_cursor() -> None:
    """Every worker's cursor must survive; none may be lost to a race."""
    tickers = [f"O:SPY{i}" for i in range(200)]
    tape = {t: [_trade(1000 + i)] for i, t in enumerate(tickers)}
    client = _FakeClient(tape)

    cursors: dict[str, int] = {}
    records: list[dict] = []
    lock = threading.Lock()

    def work(ticker: str) -> None:
        name, _raw, recs, max_ts = job._poll_ticker(client, ticker, cursors.get(ticker))
        with lock:
            records.extend(recs)
            if max_ts is not None:
                cursors[name] = max_ts

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(work, tickers))

    assert len(cursors) == len(tickers)
    assert len(records) == len(tickers)
    for i, t in enumerate(tickers):
        assert cursors[t] == 1000 + i


def test_one_failing_ticker_does_not_lose_the_others() -> None:
    """A single bad ticker must not abort the run or drop good results."""
    class _Flaky(_FakeClient):
        def paginate(self, path, params=None, limit=1000):
            if path.endswith("BOOM"):
                raise RuntimeError("upstream exploded")
            yield from super().paginate(path, params, limit)

    client = _Flaky({"O:SPY1": [_trade(5)], "O:SPY2": [_trade(7)]})
    cursors: dict[str, int] = {}
    errors: list[dict] = []
    lock = threading.Lock()

    def work(ticker: str) -> None:
        try:
            name, _raw, _recs, max_ts = job._poll_ticker(client, ticker, cursors.get(ticker))
        except Exception as exc:  # noqa: BLE001 - mirrors the job's guard
            with lock:
                errors.append({"ticker": ticker, "error": str(exc)})
            return
        with lock:
            if max_ts is not None:
                cursors[name] = max_ts

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, ["O:SPY1", "BOOM", "O:SPY2"]))

    assert len(errors) == 1
    assert cursors == {"O:SPY1": 5, "O:SPY2": 7}


# ---------------------------------------------------------------------------
# Run-level failure semantics (drives _main_fn with a stubbed watchlist/client)
# ---------------------------------------------------------------------------

def _args(**over) -> argparse.Namespace:
    return argparse.Namespace(
        date=None, limit=None, dry_run=False, underlying=None, **over
    )


def test_run_fails_when_every_ticker_errors(tmp_path: Path, monkeypatch) -> None:
    """A 100% error rate is an outage (lost entitlement, broken endpoint) and
    must fail the run -- rows=0 with a green check is how it would hide."""
    monkeypatch.setattr(job, "compute_watchlist",
                        lambda *a, **k: [{"ticker": "O:SPY1"}, {"ticker": "O:SPY2"}])

    class _BrokenClient:
        def __init__(self, *a, **k) -> None:
            pass

        def paginate(self, path, params=None, limit=1000):
            raise RuntimeError("upstream exploded")

    monkeypatch.setattr(job, "MassiveClient", _BrokenClient)
    logger = JsonlLogger(path=None, echo=False)
    with pytest.raises(RuntimeError, match="every ticker poll failed"):
        job._main_fn(_args(), _settings(tmp_path), logger)


def test_partial_failure_still_lands_good_tickers(tmp_path: Path, monkeypatch) -> None:
    """One bad ticker must not fail the run or drop the good ticker's cursor."""
    monkeypatch.setattr(job, "compute_watchlist",
                        lambda *a, **k: [{"ticker": "O:SPY1"}, {"ticker": "BOOM"}])

    class _FlakyClient:
        def __init__(self, *a, **k) -> None:
            pass

        def paginate(self, path, params=None, limit=1000):
            if path.endswith("BOOM"):
                raise RuntimeError("upstream exploded")
            return iter([_trade(42)])

    monkeypatch.setattr(job, "MassiveClient", _FlakyClient)
    settings = _settings(tmp_path)
    logger = JsonlLogger(path=None, echo=False)
    summary = job._main_fn(_args(), settings, logger)
    assert summary["rows"] == 1
    assert summary["errors"] == 1
    assert job._load_cursors(settings) == {"O:SPY1": 42}
