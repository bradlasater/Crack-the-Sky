"""Tests for ws_minute_bars: universe selection, subscribe batching, harness.

The capture harness test runs a fabricated websocket server (``websockets``)
that auths, ACKs chunked subscriptions, emits AM events and drops the first
connection — proving the supervisor reconnects and re-subscribes fully
(multi-message) and that events reach the JSONL writer. Fully offline.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from ingest import schemas
from ingest.common import landing, market_gate
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import ws_minute_bars as wsjob


def _settings(data_root: Path, ws_url: str = "ws://127.0.0.1:9") -> Settings:
    return Settings(
        massive_api_key="test-key",
        data_root=data_root,
        log_root=data_root / "logs",
        ws_delayed_url=ws_url,
    )


def _write_contracts_partition(settings: Settings, tickers: list[str], run_date) -> None:
    """Land a clean ``contracts`` partition with one record per ticker."""
    records = [
        schemas.contract_record(
            {
                "ticker": t,
                "underlying_ticker": "SPY" if t.startswith("O:SPY") else "SPX",
                "contract_type": "call",
                "exercise_style": "american",
                "expiration_date": run_date.isoformat(),
                "strike_price": 700.0 + i,
                "shares_per_contract": 100,
            }
        )
        for i, t in enumerate(tickers)
    ]
    landing.write_clean("contracts", run_date, records, job="test",
                        data_root=settings.data_root)


# ---------------------------------------------------------------------------
# Universe selection / batching
# ---------------------------------------------------------------------------

def test_subscribe_chunks_are_explicit_and_bounded():
    tickers = [f"O:SPY{i:05d}" for i in range(7)]
    chunks = wsjob.subscribe_chunks(tickers, chunk_size=3)
    assert [c.count("AM.") for c in chunks] == [3, 3, 1]
    assert chunks[0] == "AM.O:SPY00000,AM.O:SPY00001,AM.O:SPY00002"
    # every ticker subscribed exactly once, all explicit (no wildcards)
    joined = ",".join(chunks)
    assert "*" not in joined
    assert sorted(joined.replace("AM.", "").split(",")) == sorted(tickers)


def test_contract_universe_requires_partition(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(RuntimeError, match="run contracts_sync first"):
        wsjob.contract_universe(settings, market_gate.today_et(), ["SPY"])


def test_contract_universe_prefix_filter(tmp_path):
    settings = _settings(tmp_path)
    today = market_gate.today_et()
    _write_contracts_partition(
        settings,
        ["O:SPY260918C00700000", "O:SPX260918C07000000", "O:SPXW260918C07000000",
         "O:AAPL260918C00100000"],
        today,
    )
    tickers = wsjob.select_tickers(settings, today, ["SPY", "SPX"], None)
    # SPXW flows through the O:SPX prefix; other underlyings are excluded
    assert tickers == [
        "O:SPX260918C07000000", "O:SPXW260918C07000000", "O:SPY260918C00700000",
    ]
    spy_only = wsjob.select_tickers(settings, today, ["SPY"], None)
    assert spy_only == ["O:SPY260918C00700000"]


def test_select_tickers_limit_prefers_hot(tmp_path):
    settings = _settings(tmp_path)
    today = market_gate.today_et()
    tickers = [f"O:SPY260918C00{i}00000" for i in range(10)]
    _write_contracts_partition(settings, tickers, today)
    # land a SPY reference price so "hot" selection can centre on it
    bars = [{
        "ticker": "SPY", "start_ms": 1, "open": None, "high": None, "low": None,
        "close": 704.5, "volume": None, "vwap": None, "transactions": None,
    }]
    landing.write_clean("underlying_minute_bars", today, bars, job="test",
                        data_root=settings.data_root)
    picked = wsjob.select_tickers(settings, today, ["SPY"], 2)
    # strikes are 700..709; closest to 704.5 win
    assert picked == ["O:SPY260918C00400000", "O:SPY260918C00500000"]


def test_parse_events_and_ack_detection():
    frame = json.dumps([
        {"ev": "status", "status": "success", "message": "subscribed to: AM.O:SPY1"},
        {"ev": "AM", "sym": "O:SPY1", "v": 10, "av": 10, "op": 1.0, "vw": 1.0,
         "o": 1.0, "c": 1.1, "h": 1.2, "l": 0.9, "a": 1.05, "z": 3,
         "s": 111, "e": 222},
    ])
    assert wsjob.is_subscribe_ack(frame)
    recs = wsjob.parse_events(frame)
    assert len(recs) == 1 and recs[0]["sym"] == "O:SPY1"
    assert recs[0]["s"] == 111 and recs[0]["e"] == 222 and "recv_ms" in recs[0]
    assert not wsjob.is_subscribe_ack(json.dumps([{"ev": "AM", "sym": "X"}]))


# ---------------------------------------------------------------------------
# Fabricated-server harness: reconnect + full multi-message re-subscribe
# ---------------------------------------------------------------------------

def test_capture_reconnects_and_resubscribes(tmp_path, monkeypatch):
    from websockets.asyncio.server import serve

    monkeypatch.setattr(wsjob, "HEARTBEAT_TIMEOUT_S", 0.5)
    monkeypatch.setattr(wsjob, "BACKOFF_MIN_S", 0.1)
    monkeypatch.setattr(wsjob, "BACKOFF_MAX_S", 0.5)

    tickers = [f"O:SPY260918C00{i}00000" for i in range(5)]
    chunks = wsjob.subscribe_chunks(tickers, chunk_size=2)  # 3 messages
    assert len(chunks) == 3

    connections: list[dict] = []

    async def handler(ws):
        state = {"subscribes": [], "authed": False}
        connections.append(state)
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("action") == "auth":
                    state["authed"] = True
                    await ws.send(json.dumps(
                        [{"ev": "status", "status": "auth_success"}]))
                elif msg.get("action") == "subscribe":
                    state["subscribes"].append(msg["params"])
                    await ws.send(json.dumps([{
                        "ev": "status", "status": "success",
                        "message": f"subscribed to: {msg['params']}",
                    }]))
                    await ws.send(json.dumps([{
                        "ev": "AM", "sym": tickers[0], "v": 5, "av": 5,
                        "op": 1.0, "vw": 1.0, "o": 1.0, "c": 1.0, "h": 1.0,
                        "l": 1.0, "a": 1.0, "z": 1, "s": 1, "e": 2,
                    }]))
                if len(state["subscribes"]) == len(chunks) and len(connections) == 1:
                    await ws.close()  # drop the first connection -> reconnect
        finally:
            pass

    async def run() -> dict:
        server = await serve(handler, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            settings = _settings(tmp_path, ws_url=f"ws://127.0.0.1:{port}")
            logger = JsonlLogger(echo=False)
            writer = wsjob.HourlyJsonlWriter(tmp_path, market_gate.today_et(), logger)
            writer.start()
            try:
                deadline = market_gate.now_et() + timedelta(seconds=4)
                return await wsjob._capture(settings, logger, market_gate.today_et(),
                                            deadline, writer, chunks)
            finally:
                writer.stop()
                writer.join(timeout=10)
                logger.close()
        finally:
            server.close()

    stats = asyncio.run(asyncio.wait_for(run(), timeout=30))

    # reconnect happened and every connection got the full multi-message
    # subscription (auth -> 3 chunked subscribe messages, in order)
    assert stats["reconnects"] >= 1
    assert len(connections) >= 2
    for state in connections:
        assert state["authed"]
        assert state["subscribes"] == chunks
    # AM events from both connections reached the writer
    assert stats["events"] >= 2
    assert stats["distinct_symbols"] >= 1


# ---------------------------------------------------------------------------
# Reading the capture back
# ---------------------------------------------------------------------------
#
# parse_events reads a *frame off the wire*; the writer persists records that
# have already been projected onto AM_FIELDS, so the ev discriminator is gone
# by the time they hit disk. Feeding a persisted line to parse_events silently
# yields nothing, which is exactly what reconcile did -- reporting ws_rows=0
# on days the capture worked perfectly.

def test_parse_persisted_line_reads_a_writer_projected_record():
    line = json.dumps({
        "sym": "O:SPY260930C00780000", "v": 10, "av": 2416, "op": 3.18,
        "vw": 3.47, "o": 3.47, "c": 3.47, "h": 3.47, "l": 3.47, "a": 3.23,
        "z": 2, "s": 1788277440000, "e": 1788277500000, "recv_ms": 1788278402085,
    })
    recs = wsjob.parse_persisted_line(line)
    assert len(recs) == 1
    assert recs[0]["sym"] == "O:SPY260930C00780000"
    assert recs[0]["v"] == 10


def test_parse_events_would_have_dropped_that_line():
    """The bug, pinned: the projection has no ``ev`` for parse_events to match."""
    line = json.dumps({"sym": "O:SPY260930C00780000", "v": 10})
    assert wsjob.parse_events(line) == []
    assert len(wsjob.parse_persisted_line(line)) == 1


def test_parse_persisted_line_still_reads_a_legacy_raw_frame():
    """Archives written before the projection existed must keep reading."""
    frame = json.dumps([
        {"ev": "AM", "sym": "O:SPX260901P07425000", "v": 50, "s": 1, "e": 2},
        {"ev": "status", "status": "connected"},
    ])
    recs = wsjob.parse_persisted_line(frame)
    assert len(recs) == 1
    assert recs[0]["sym"] == "O:SPX260901P07425000"


@pytest.mark.parametrize("line", [
    "", "   ", "not json", "[", "null", "123", '"a string"',
    json.dumps([1, 2, 3]),
    json.dumps({"v": 10}),            # no sym: not attributable to a contract
    json.dumps({"sym": "", "v": 10}),  # empty sym, likewise
])
def test_parse_persisted_line_rejects_malformed_input(line):
    assert wsjob.parse_persisted_line(line) == []


def test_writer_output_round_trips_through_the_reader(tmp_path):
    """The two halves must agree: whatever the writer writes, the reader reads."""
    logger = JsonlLogger(path=None, echo=False)
    writer = wsjob.HourlyJsonlWriter(tmp_path, date(2026, 9, 1), logger)
    writer.start()
    records = wsjob.parse_events(json.dumps([
        {"ev": "AM", "sym": "O:SPY1", "v": 3, "s": 1, "e": 2},
        {"ev": "AM", "sym": "O:SPY2", "v": 7, "s": 1, "e": 2},
    ]))
    for rec in records:
        writer.queue.put(rec)
    writer.stop()
    writer.join(timeout=10)

    part = tmp_path / "raw" / wsjob.DATASET / "dt=2026-09-01"
    lines = [
        line
        for f in sorted(part.glob("*.jsonl*"))
        for line in _read_lines(f)
    ]
    read_back = [r for line in lines for r in wsjob.parse_persisted_line(line)]
    assert [r["sym"] for r in read_back] == ["O:SPY1", "O:SPY2"]
    assert sum(r["v"] for r in read_back) == 10


def _read_lines(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [line for line in fh if line.strip()]
