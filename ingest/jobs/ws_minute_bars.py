"""Supervised websocket capture of delayed option minute bars (AM events).

Connects to ``wss://delayed.massive.com/options`` (one connection only — the
plan allows a single slot per asset class), authenticates, subscribes to
``AM.O:SPY*,AM.O:SPX*`` and lands every message as JSONL under
``raw/option_minute_bars_ws/dt=YYYY-MM-DD/`` (rotated + gzipped hourly).

Design notes (per SPEC verified facts):
  * Messages arrive as JSON *arrays* of events keyed by ``ev``; AM events
    carry ``sym,v,av,op,vw,o,c,h,l,a,z,s,e`` (s/e = window start/end ns).
  * The server disconnects slow consumers, so the reader loop never blocks:
    raw payloads go onto a ``queue.Queue`` drained by a writer thread.
  * Reconnects use exponential backoff (1s -> 60s) with jitter, then
    re-authenticate and re-subscribe; each outage is logged as ``ws_gap``.
  * Heartbeat: if no message arrives for 90s the connection is recycled.
  * Capture window: 09:25 ET -> ``market_gate.option_capture_end_et``
    (~16:35 ET). Outside the window the job exits 0 unless ``--force``.
  * ``--duration-minutes N`` overrides the window end (testing).

Run: ``python -m ingest.jobs.ws_minute_bars [--force] [--duration-minutes N]``
"""

from __future__ import annotations

import asyncio
import gzip
import json
import queue
import random
import sys
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

import websockets

from ingest.common import market_gate
from ingest.common.cli import build_parser
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger, get_run_logger

JOB = "ws_minute_bars"
DATASET = "option_minute_bars_ws"
SUBSCRIBE_PARAMS = "AM.O:SPY*,AM.O:SPX*"
WINDOW_START = dtime(9, 25)  # ET
HEARTBEAT_TIMEOUT_S = 90
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0

# Fields delivered on AM aggregate events (verified against the live feed).
AM_FIELDS = ("sym", "v", "av", "op", "vw", "o", "c", "h", "l", "a", "z", "s", "e")


def parse_events(payload: str | bytes) -> list[dict[str, Any]]:
    """Parse one WS frame (a JSON array of events) into event dicts.

    Only ``ev == "AM"`` events are returned, projected onto the verified AM
    field set with an added ``recv_ms`` receipt timestamp. Non-list payloads
    and non-AM events (e.g. ``status``) yield no records.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):  # tolerate single-object frames
        data = [data]
    if not isinstance(data, list):
        return []
    recv_ms = int(time.time() * 1000)
    out: list[dict[str, Any]] = []
    for ev in data:
        if not isinstance(ev, dict) or ev.get("ev") != "AM":
            continue
        rec = {k: ev.get(k) for k in AM_FIELDS}
        rec["recv_ms"] = recv_ms
        out.append(rec)
    return out


def describe_events(payload: str | bytes) -> list[str]:
    """Return the ``ev``/``status`` markers of a frame (for logging)."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return ["unparseable"]
    if isinstance(data, dict):
        data = [data]
    return [str(e.get("ev") or e.get("status") or "?") for e in data if isinstance(e, dict)]


class HourlyJsonlWriter(threading.Thread):
    """Writer thread: drains a queue of AM records into hourly JSONL files.

    Files live at ``raw/option_minute_bars_ws/dt=<date>/`` and rotate on the
    hour; the just-closed file is gzip-compressed to ``<name>.gz``.
    """

    def __init__(self, data_root: Path, run_date: date, logger: JsonlLogger) -> None:
        super().__init__(daemon=True, name=f"{JOB}-writer")
        self.data_root = Path(data_root)
        self.run_date = run_date
        self.logger = logger
        self.queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.rows_written = 0
        self._fh: Any = None
        self._path: Path | None = None
        self._hour: int | None = None

    def _out_dir(self) -> Path:
        out = self.data_root / "raw" / DATASET / f"dt={self.run_date.isoformat()}"
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _open(self, hour: int) -> None:
        ts_ms = int(time.time() * 1000)
        self._path = self._out_dir() / f"{JOB}-{ts_ms}.jsonl"
        self._fh = open(self._path, "a", encoding="utf-8")
        self._hour = hour
        self.logger.log("ws_writer_open", path=str(self._path))

    def _close(self) -> None:
        if self._fh is None:
            return
        self._fh.close()
        self._fh = None
        if self._path is not None and self._path.exists():
            gz_path = self._path.with_suffix(self._path.suffix + ".gz")
            with open(self._path, "rb") as src, gzip.open(gz_path, "wb") as dst:
                dst.writelines(src)
            self._path.unlink()
            self.logger.log("ws_writer_rotate", gzipped=str(gz_path))
        self._path = None
        self._hour = None

    def run(self) -> None:  # noqa: D102 - thread entry point
        while True:
            item = self.queue.get()
            try:
                if item is None:  # sentinel: shutdown
                    return
                hour = datetime.now(market_gate.ET).hour
                if self._fh is None or hour != self._hour:
                    self._close()
                    self._open(hour)
                self._fh.write(json.dumps(item, default=str) + "\n")
                self.rows_written += 1
                if self.rows_written % 1000 == 0:
                    self._fh.flush()
            finally:
                self.queue.task_done()

    def stop(self) -> None:
        """Drain the queue and close (gzipping) the current file."""
        self.queue.put(None)
        self.queue.join()
        self._close()


async def _auth_and_subscribe(ws: Any, settings: Settings, logger: JsonlLogger) -> bool:
    """Authenticate then subscribe; returns True on success.

    Expects an ``auth_success`` status event; ``auth_failed`` is fatal
    (returns False so the job exits instead of hammering the API).
    """
    await ws.send(json.dumps({"action": "auth", "params": settings.massive_api_key}))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=30)
        markers = describe_events(raw)
        logger.log("ws_status", markers=markers)
        try:
            events = json.loads(raw)
        except ValueError:
            continue
        if isinstance(events, dict):
            events = [events]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            status = ev.get("status")
            if status == "auth_success":
                await ws.send(json.dumps({"action": "subscribe", "params": SUBSCRIBE_PARAMS}))
                logger.log("ws_subscribed", params=SUBSCRIBE_PARAMS)
                return True
            if status == "auth_failed":
                logger.log("ws_auth_failed", message=ev.get("message"))
                return False
    logger.log("ws_auth_timeout")
    return False


async def _read_loop(ws: Any, writer: HourlyJsonlWriter, logger: JsonlLogger,
                     deadline: datetime, stats: dict[str, int]) -> None:
    """Read frames until the deadline, a close, or a heartbeat timeout.

    The recv timeout is capped at the time remaining until ``deadline`` so
    the job ends promptly at the window close even when the feed is quiet.
    ``stats["frames"]`` is incremented per received frame (exceptions
    propagate to the reconnect supervisor without losing the count).
    """
    while True:
        remaining = (deadline - market_gate.now_et()).total_seconds()
        if remaining <= 0:
            return
        try:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=min(HEARTBEAT_TIMEOUT_S, remaining + 1)
            )
        except asyncio.TimeoutError:
            if market_gate.now_et() >= deadline:
                return  # quiet feed at window close, not a heartbeat loss
            raise
        stats["frames"] += 1
        for rec in parse_events(raw):
            writer.queue.put(rec)


async def _capture(settings: Settings, logger: JsonlLogger, run_date: date,
                   deadline: datetime, writer: HourlyJsonlWriter) -> dict[str, int]:
    """Connection supervisor: connect, auth, read, reconnect with backoff."""
    stats = {"connects": 0, "reconnects": 0, "frames": 0}
    backoff = BACKOFF_MIN_S
    url = settings.ws_delayed_url

    while market_gate.now_et() < deadline:
        gap_from = market_gate.now_et().isoformat()
        try:
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=30, max_queue=10000
            ) as ws:
                stats["connects"] += 1
                logger.log("ws_connected", url=url)
                if not await _auth_and_subscribe(ws, settings, logger):
                    # auth_failed: wrong key — retrying will never help
                    raise _FatalAuth("websocket auth_failed; check MASSIVE_API_KEY")
                backoff = BACKOFF_MIN_S  # healthy connection resets backoff
                await _read_loop(ws, writer, logger, deadline, stats)
        except _FatalAuth:
            raise
        except asyncio.TimeoutError:
            logger.log("ws_heartbeat_timeout", timeout_s=HEARTBEAT_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect supervisor
            logger.log("ws_disconnected", error=f"{type(exc).__name__}: {exc}")
        if market_gate.now_et() >= deadline:
            break
        if stats["connects"]:
            stats["reconnects"] += 1
        logger.log("ws_gap", **{"from": gap_from, "to": market_gate.now_et().isoformat()})
        sleep_s = min(backoff, BACKOFF_MAX_S) * (0.5 + random.random())
        logger.log("ws_reconnect_wait", sleep_s=round(sleep_s, 2))
        await asyncio.sleep(sleep_s)
        backoff = min(backoff * 2, BACKOFF_MAX_S)
    return stats


class _FatalAuth(Exception):
    """Raised when the websocket rejects the API key (no point retrying)."""


def _window(run_date: date, settings: Settings, force: bool,
            duration_minutes: int | None, logger: JsonlLogger) -> datetime | None:
    """Compute the capture deadline (ET); None means 'exit 0 now'.

    Before 09:25 the job sleeps until the window opens; after
    ``option_capture_end_et`` it exits quietly unless ``--force``.
    ``--duration-minutes`` overrides the end relative to *now*.
    """
    now = market_gate.now_et()
    if duration_minutes is not None:
        return now + timedelta(minutes=duration_minutes)
    start = datetime.combine(run_date, WINDOW_START, tzinfo=market_gate.ET)
    end = market_gate.option_capture_end_et(run_date, settings.data_root)
    if now >= end and not force:
        logger.log("ws_window_closed", now=now.isoformat(), end=end.isoformat())
        return None
    if now < start:
        if force:
            return end
        wait_s = (start - now).total_seconds()
        logger.log("ws_window_wait", start=start.isoformat(), wait_s=round(wait_s, 1))
        time.sleep(max(wait_s, 0))
    return end


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m ingest.jobs.ws_minute_bars``."""
    parser = build_parser(JOB)
    parser.add_argument("--duration-minutes", type=int, default=None,
                        help="override capture length in minutes (testing)")
    args = parser.parse_args(argv)
    run_date = date.fromisoformat(args.date) if args.date else market_gate.today_et()

    settings = Settings.load()
    logger = get_run_logger(JOB, run_date, log_root=settings.log_root)
    started = time.monotonic()
    try:
        logger.log("job_start", job=JOB, date=run_date.isoformat(), force=args.force,
                   duration_minutes=args.duration_minutes)
        market_gate.require_trading_day(run_date, force=args.force,
                                        data_root=settings.data_root)
        deadline = _window(run_date, settings, args.force, args.duration_minutes, logger)
        if deadline is None:
            return 0
        writer = HourlyJsonlWriter(settings.data_root, run_date, logger)
        writer.start()
        try:
            stats = asyncio.run(_capture(settings, logger, run_date, deadline, writer))
        finally:
            writer.stop()
            writer.join(timeout=30)
        duration_s = round(time.monotonic() - started, 3)
        logger.log("job_end", job=JOB, rows=writer.rows_written, duration_s=duration_s, **stats)
        return 0
    except _FatalAuth as exc:
        logger.log("job_error", job=JOB, error=str(exc))
        return 1
    except KeyboardInterrupt:
        logger.log("job_interrupted", job=JOB)
        return 0
    finally:
        logger.close()


if __name__ == "__main__":
    sys.exit(main())
