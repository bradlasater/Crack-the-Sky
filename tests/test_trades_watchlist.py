"""Tests for the concurrent trades poll and its cursor handling.

Cursors are the resume state: if concurrent workers corrupt or lose them, the
next run silently re-fetches or (worse) skips trades. These lock down the
merge semantics without touching the network.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ingest.common import landing
from ingest.common.config import Settings
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
# Adaptive backoff
#
# The watchlist is ~9,100 tickers but a median slot has trades in ~850 of
# them, so the job spent 248s of a 300s slot on requests that returned
# nothing. Backoff skips persistently silent tickers. The property that makes
# that safe -- skipping never drops a trade, because the cursor is untouched
# -- is what most of these tests are actually about.
# ---------------------------------------------------------------------------

def test_backoff_ladder_is_linear_to_the_cap() -> None:
    # A ticker that just traded, or has been quiet once, is polled next slot.
    assert job._next_due(0, 10) == [0, 11]
    assert job._next_due(1, 10) == [1, 11]
    # Then the gap widens with the silence, capped so nothing is ever
    # forgotten for longer than BACKOFF_MAX slots.
    assert job._next_due(2, 10) == [2, 12]
    assert job._next_due(3, 10) == [3, 13]
    assert job._next_due(99, 10) == [99, 10 + job.BACKOFF_MAX]


def test_unknown_tickers_are_always_polled() -> None:
    """A new contract in the watchlist must not inherit anyone's silence."""
    state = {"O:OLD": [5, 100]}
    due = job._due_tickers(["O:OLD", "O:NEW"], run_index=1, state=state)
    assert due == ["O:NEW"]


def test_due_tickers_polls_when_the_slot_arrives() -> None:
    state = {"O:A": [3, 12], "O:B": [3, 13]}
    assert job._due_tickers(["O:A", "O:B"], 12, state) == ["O:A"]
    assert job._due_tickers(["O:A", "O:B"], 13, state) == ["O:A", "O:B"]


def test_poll_state_round_trips(tmp_path: Path) -> None:
    from datetime import date

    s = _settings(tmp_path)
    day = date(2026, 9, 4)
    job._save_poll_state(s, day, 7, {"O:SPY1": [2, 9]})
    assert job._load_poll_state(s, day) == (7, {"O:SPY1": [2, 9]})


def test_poll_state_from_another_session_is_discarded(tmp_path: Path) -> None:
    """The open is the worst possible moment to inherit yesterday's silence."""
    from datetime import date

    s = _settings(tmp_path)
    job._save_poll_state(s, date(2026, 9, 3), 80, {"O:SPY1": [9, 84]})
    assert job._load_poll_state(s, date(2026, 9, 4)) == (0, {})


def test_corrupt_poll_state_polls_everything(tmp_path: Path) -> None:
    """This file is an optimisation hint; its failure mode must be more work."""
    from datetime import date

    s = _settings(tmp_path)
    landing.meta_path(job.POLL_STATE_NAME, data_root=tmp_path).write_text(
        "{not json", encoding="utf-8"
    )
    run, state = job._load_poll_state(s, date(2026, 9, 4))
    assert (run, state) == (0, {})
    assert job._due_tickers(["O:A", "O:B"], run + 1, state) == ["O:A", "O:B"]


def test_missing_poll_state_polls_everything(tmp_path: Path) -> None:
    from datetime import date

    s = _settings(tmp_path)
    assert job._load_poll_state(s, date(2026, 9, 4)) == (0, {})


def _run_slot(monkeypatch, settings, tape: dict[str, list[dict]], tickers: list[str]):
    """Run one full _main_fn slot against a fake tape; return (result, polled)."""
    import argparse

    from ingest.common.logging_utils import JsonlLogger

    client = _FakeClient(tape)
    monkeypatch.setattr(
        job, "compute_watchlist", lambda *a, **k: [{"ticker": t} for t in tickers]
    )
    monkeypatch.setattr(job, "MassiveClient", lambda *a, **k: client)
    args = argparse.Namespace(
        date="2026-09-04", limit=None, dry_run=False, force=True, underlying=None
    )
    result = job._main_fn(args, settings, JsonlLogger(path=None, echo=False))
    return result, [t for t, _ in client.seen]


def test_silent_tickers_are_skipped_after_the_ladder_ramps(monkeypatch, tmp_path) -> None:
    """The whole point: stop spending the slot on tickers with nothing to say."""
    s = _settings(tmp_path)
    tickers = ["O:HOT", "O:COLD"]
    # O:HOT trades every slot; O:COLD never does.
    tape = {"O:HOT": [_trade(1)], "O:COLD": []}

    polled_per_slot = []
    for slot in range(6):
        tape["O:HOT"] = [_trade(slot + 1)]
        _res, polled = _run_slot(monkeypatch, s, tape, tickers)
        polled_per_slot.append(sorted(polled))

    # The hot ticker is polled in every single slot.
    assert all("O:HOT" in p for p in polled_per_slot)
    # The cold one ramps out: polled at first, then progressively skipped.
    cold = [("O:COLD" in p) for p in polled_per_slot]
    assert cold[0] is True
    assert cold.count(False) >= 2, f"cold ticker never backed off: {cold}"


def test_backoff_delays_a_trade_but_never_drops_it(monkeypatch, tmp_path) -> None:
    """The safety property the whole design rests on.

    A skipped poll does not advance the cursor, so when the ticker is finally
    polled it still asks for everything since the last trade actually seen.
    """
    s = _settings(tmp_path)
    tickers = ["O:QUIET"]
    tape: dict[str, list[dict]] = {"O:QUIET": []}

    # Several empty slots drive O:QUIET down the ladder.
    for _ in range(5):
        _run_slot(monkeypatch, s, tape, tickers)

    # It now trades while backed off.
    tape["O:QUIET"] = [_trade(4242)]
    seen_rows = 0
    for _ in range(job.BACKOFF_MAX + 1):
        res, _polled = _run_slot(monkeypatch, s, tape, tickers)
        seen_rows += res["rows"]

    # Delayed by at most the cap, but delivered -- and the cursor advanced.
    assert seen_rows == 1, "a trade was dropped by backoff"
    cursors = json.loads(
        landing.meta_path(job.CURSOR_NAME, data_root=tmp_path).read_text()
    )
    assert cursors["O:QUIET"] == 4242


def test_disabling_backoff_polls_everything(monkeypatch, tmp_path) -> None:
    """TRADES_BACKOFF_MAX=1 must restore the pre-2026-09 behaviour exactly."""
    monkeypatch.setattr(job, "BACKOFF_MAX", 1)
    s = _settings(tmp_path)
    tickers = ["O:A", "O:B", "O:C"]
    for _ in range(5):
        _res, polled = _run_slot(monkeypatch, s, {}, tickers)
        assert sorted(polled) == tickers


def test_state_prunes_tickers_that_left_the_watchlist(monkeypatch, tmp_path) -> None:
    from datetime import date

    s = _settings(tmp_path)
    _run_slot(monkeypatch, s, {}, ["O:A", "O:B"])
    _run_slot(monkeypatch, s, {}, ["O:A"])
    _run, state = job._load_poll_state(s, date(2026, 9, 4))
    assert set(state) == {"O:A"}


def test_failed_polls_are_retried_next_slot_not_treated_as_silence(
    monkeypatch, tmp_path
) -> None:
    """An upstream error is not evidence that a ticker has nothing to say."""
    import argparse

    from ingest.common.logging_utils import JsonlLogger

    class _Boom(_FakeClient):
        def paginate(self, path, params=None, limit=1000):
            ticker = path.rsplit("/", 1)[-1]
            self.seen.append((ticker, None))
            raise RuntimeError("upstream exploded")
            yield  # pragma: no cover

    s = _settings(tmp_path)
    monkeypatch.setattr(
        job, "compute_watchlist", lambda *a, **k: [{"ticker": "O:ERR"}]
    )
    args = argparse.Namespace(
        date="2026-09-04", limit=None, dry_run=False, force=True, underlying=None
    )
    for _ in range(4):
        monkeypatch.setattr(job, "MassiveClient", lambda *a, **k: _Boom({}))
        res = job._main_fn(args, s, JsonlLogger(path=None, echo=False))
        assert res["polled"] == 1, "an erroring ticker was backed off"
        assert res["errors"] == 1
