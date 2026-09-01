"""Supervised websocket capture of delayed option minute bars (AM events).

Connects to ``wss://delayed.massive.com/options`` (one connection only — the
plan allows a single slot per asset class), authenticates, subscribes to
**explicit** AM channels for every active SPY/SPX option contract and lands
every message as JSONL under ``raw/option_minute_bars_ws/dt=YYYY-MM-DD/``
(rotated + gzipped hourly).

Why explicit subscriptions: live probes proved the wildcard
``AM.O:SPY*,AM.O:SPX*`` subscribes successfully but delivers ZERO events on
this tier, while an explicit ``AM.O:<TICKER>`` subscription delivers bars.
The contract universe therefore comes from the latest ``clean/contracts``
partition (run ``contracts_sync`` first); tickers are subscribed in chunks of
``SUBSCRIBE_CHUNK_SIZE`` per ``subscribe`` message (well under the 1MB frame
limit) over the single connection, and fully re-subscribed on reconnect.

Design notes (per SPEC verified facts):
  * Messages arrive as JSON *arrays* of events keyed by ``ev``; AM events
    carry ``sym,v,av,op,vw,o,c,h,l,a,z,s,e`` (s/e = window start/end ns).
  * The server disconnects slow consumers, so the reader loop never blocks:
    raw payloads go onto a ``queue.Queue`` drained by a writer thread.
  * Reconnects use exponential backoff (1s -> 60s) with jitter, then
    re-authenticate and re-subscribe; each outage is logged as ``ws_gap``.
  * Heartbeat: if no message arrives for 90s *before the subscription is
    ACKed* the connection is recycled. Once the subscription is ACKed, a
    quiet feed is NOT an error — silence is surfaced via the periodic
    ``ws_stats`` log event (events / distinct_symbols / queue_depth every
    5 minutes) instead of a reconnect loop.
  * Capture window: 09:25 ET -> ``market_gate.option_capture_end_et``
    (~16:35 ET). Outside the window the job exits 0 unless ``--force``.
  * ``--duration-minutes N`` overrides the window end (testing);
    ``--contracts-limit N`` caps the universe for tests (hot contracts
    first when a SPY reference price is available).

Run: ``python -m ingest.jobs.ws_minute_bars [--underlying SPY] [--force]``
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
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

import websockets

from ingest.common import market_gate
from ingest.common.cli import build_parser, healthcheck_url, ping
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger, get_run_logger
from ingest.jobs import latest_contracts, latest_spy_price, parse_underlyings

JOB = "ws_minute_bars"
DATASET = "option_minute_bars_ws"
DEFAULT_UNDERLYINGS = ["SPY", "SPX"]  # SPXW contracts share the O:SPX prefix
SUBSCRIBE_CHUNK_SIZE = 3000  # tickers per subscribe message (<< 1MB limit)
WINDOW_START = dtime(9, 25)  # ET
HEARTBEAT_TIMEOUT_S = 90
STATS_INTERVAL_S = 300  # periodic ws_stats log cadence
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0

# Fields delivered on AM aggregate events (verified against the live feed).
AM_FIELDS = ("sym", "v", "av", "op", "vw", "o", "c", "h", "l", "a", "z", "s", "e")


# ---------------------------------------------------------------------------
# Contract universe / subscription batching
# ---------------------------------------------------------------------------

def _ticker_prefix(underlying: str) -> str:
    """OPRA ticker prefix for an underlying (``I:SPX``/``SPX`` -> ``O:SPX``)."""
    u = underlying.strip().upper()
    if u.startswith("I:"):
        u = u[2:]
    return f"O:{u}"


def contract_universe(
    settings: Settings, run_date: date, underlyings: list[str]
) -> list[dict[str, Any]]:
    """Active contracts for the underlyings from the latest clean partition.

    Raises RuntimeError ("run contracts_sync first") when no ``contracts``
    clean partition exists at or before ``run_date`` — without the reference
    universe we cannot build explicit subscriptions.
    """
    contracts = latest_contracts(settings, run_date)
    if not contracts:
        raise RuntimeError(
            "no clean 'contracts' partition found at or before "
            f"{run_date}; run contracts_sync first"
        )
    prefixes = tuple(_ticker_prefix(u) for u in underlyings)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for rec in contracts:
        ticker = rec.get("ticker")
        if not ticker or not str(ticker).startswith(prefixes) or ticker in seen:
            continue
        seen.add(ticker)
        out.append(rec)
    return out


def select_tickers(
    settings: Settings,
    run_date: date,
    underlyings: list[str],
    limit: int | None,
    logger: JsonlLogger | None = None,
) -> list[str]:
    """Subscription tickers for the universe, optionally capped by ``limit``.

    With a limit, "hot" contracts are preferred: nearest expiration first,
    then strike closest to the latest SPY reference price (from
    ``underlying_minute_bars`` or ``option_snapshots``), so short test runs
    actually see AM traffic. Without a reference price the first ``limit``
    tickers in ticker order are used.
    """
    universe = contract_universe(settings, run_date, underlyings)
    mode = "full"
    if limit is not None and len(universe) > limit:
        ref = latest_spy_price(settings, run_date)
        if ref is not None:
            def hot_key(rec: dict[str, Any]) -> tuple[str, float, str]:
                strike = rec.get("strike_price")
                dist = abs(float(strike) - ref) if strike is not None else float("inf")
                return (str(rec.get("expiration_date") or "9999"), dist, str(rec["ticker"]))

            universe = sorted(universe, key=hot_key)[:limit]
            mode = f"hot(ref={ref})"
        else:
            universe = sorted(universe, key=lambda r: str(r["ticker"]))[:limit]
            mode = "first-n"
    tickers = sorted(str(r["ticker"]) for r in universe)
    if logger is not None:
        logger.log(
            "ws_universe",
            underlyings=underlyings,
            contracts=len(tickers),
            limit=limit,
            selection=mode,
        )
    if not tickers:
        raise RuntimeError(
            f"contract universe for {underlyings} is empty; run contracts_sync first"
        )
    return tickers


def subscribe_chunks(
    tickers: list[str], chunk_size: int = SUBSCRIBE_CHUNK_SIZE
) -> list[str]:
    """``params`` strings for explicit AM subscriptions, chunked per message."""
    return [
        ",".join(f"AM.{t}" for t in tickers[i:i + chunk_size])
        for i in range(0, len(tickers), chunk_size)
    ]


# ---------------------------------------------------------------------------
# Frame parsing / logging helpers
# ---------------------------------------------------------------------------

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


def is_subscribe_ack(payload: str | bytes) -> bool:
    """True when the frame carries a successful subscription status event."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return False
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return False
    for ev in data:
        if not isinstance(ev, dict) or ev.get("ev") != "status":
            continue
        text = f"{ev.get('status', '')} {ev.get('message', '')}".lower()
        if "subscribed" in text:
            return True
    return False


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
        # Held open across the capture hour; closed (and gzipped) by _close().
        self._fh = open(self._path, "a", encoding="utf-8")  # noqa: SIM115
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


async def _auth_and_subscribe(
    ws: Any, settings: Settings, logger: JsonlLogger, chunks: list[str]
) -> bool:
    """Authenticate then send every chunked subscribe message; True on success.

    Expects an ``auth_success`` status event; ``auth_failed`` is fatal
    (returns False so the job exits instead of hammering the API). Each
    subscribe message stays well under the 1MB server frame limit.
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
                for i, params in enumerate(chunks, start=1):
                    await ws.send(json.dumps({"action": "subscribe", "params": params}))
                    logger.log(
                        "ws_subscribed",
                        message=f"{i}/{len(chunks)}",
                        tickers=params.count("AM."),
                    )
                return True
            if status == "auth_failed":
                logger.log("ws_auth_failed", message=ev.get("message"))
                return False
    logger.log("ws_auth_timeout")
    return False


async def _read_loop(
    ws: Any,
    writer: HourlyJsonlWriter,
    logger: JsonlLogger,
    deadline: datetime,
    stats: dict[str, Any],
) -> None:
    """Read frames until the deadline, a close, or a pre-ACK heartbeat timeout.

    The recv timeout is capped at the time remaining until ``deadline`` so
    the job ends promptly at the window close even when the feed is quiet.

    Zero-data handling: once the subscription is ACKed (or any traffic has
    flowed), a 90s silence is NOT a failure — it is logged (``ws_silence``)
    and reading continues; only a silent *unacknowledged* subscription is
    recycled. A ``ws_stats`` snapshot (events, distinct_symbols, queue_depth)
    is logged every ``STATS_INTERVAL_S`` regardless, so a dead-quiet feed is
    always visible in the run log.
    """
    last_stats_log = time.monotonic()

    def log_stats(event: str = "ws_stats") -> None:
        logger.log(
            event,
            events=stats["events"],
            distinct_symbols=len(stats["symbols"]),
            queue_depth=writer.queue.qsize(),
        )

    while True:
        remaining = (deadline - market_gate.now_et()).total_seconds()
        if remaining <= 0:
            log_stats()
            return
        try:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=min(HEARTBEAT_TIMEOUT_S, remaining + 1)
            )
        except TimeoutError:
            if market_gate.now_et() >= deadline:
                return  # quiet feed at window close, not a heartbeat loss
            if stats["acked"]:
                # Zero data with an active subscription: log, keep waiting.
                logger.log("ws_silence", timeout_s=HEARTBEAT_TIMEOUT_S)
                log_stats()
                last_stats_log = time.monotonic()
                continue
            raise  # subscription never ACKed: recycle the connection
        stats["frames"] += 1
        if is_subscribe_ack(raw):
            stats["acked"] = True
        for rec in parse_events(raw):
            stats["events"] += 1
            if rec.get("sym"):
                stats["acked"] = True  # traffic implies a live subscription
                stats["symbols"].add(rec["sym"])
            writer.queue.put(rec)
        if time.monotonic() - last_stats_log >= STATS_INTERVAL_S:
            log_stats()
            last_stats_log = time.monotonic()


async def _capture(
    settings: Settings,
    logger: JsonlLogger,
    run_date: date,
    deadline: datetime,
    writer: HourlyJsonlWriter,
    chunks: list[str],
) -> dict[str, Any]:
    """Connection supervisor: connect, auth, subscribe, read, reconnect."""
    stats: dict[str, Any] = {
        "connects": 0, "reconnects": 0, "frames": 0, "events": 0,
        "acked": False, "symbols": set(),
    }
    backoff = BACKOFF_MIN_S
    url = settings.ws_delayed_url

    while market_gate.now_et() < deadline:
        gap_from = market_gate.now_et().isoformat()
        try:
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=30, max_queue=10000
            ) as ws:
                stats["connects"] += 1
                stats["acked"] = False
                logger.log("ws_connected", url=url)
                if not await _auth_and_subscribe(ws, settings, logger, chunks):
                    # auth_failed: wrong key — retrying will never help
                    raise _FatalAuth("websocket auth_failed; check MASSIVE_API_KEY")
                backoff = BACKOFF_MIN_S  # healthy connection resets backoff
                await _read_loop(ws, writer, logger, deadline, stats)
        except _FatalAuth:
            raise
        except TimeoutError:
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
    return {
        "connects": stats["connects"],
        "reconnects": stats["reconnects"],
        "frames": stats["frames"],
        "events": stats["events"],
        "distinct_symbols": len(stats["symbols"]),
    }


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
    parser.add_argument("--contracts-limit", type=int, default=None,
                        help="cap the contract universe (testing; hot contracts "
                             "preferred when a SPY reference price is available)")
    args = parser.parse_args(argv)
    run_date = date.fromisoformat(args.date) if args.date else market_gate.today_et()

    settings = Settings.load()
    logger = get_run_logger(JOB, run_date, log_root=settings.log_root)
    started = time.monotonic()
    # This job does not go through cli.run_job, so it wires its own pings --
    # without them the one job that has historically produced nothing would
    # also be the one job that never reported.
    ping_url, autocreate = healthcheck_url(settings, JOB)
    ping(ping_url, "/start", autocreate)
    # Every exit path below must send a terminal ping. Several used not to --
    # the market gate's SystemExit(0), a closed capture window, a missing
    # contract universe, Ctrl-C, an unexpected exception -- and each of those
    # left the check "started" until Healthchecks called it a hung run.
    settled = False

    def settle(ok: bool, message: str) -> None:
        nonlocal settled
        if settled:
            return
        settled = True
        ping(ping_url, "" if ok else "/fail", autocreate, body=f"{JOB} {message}")

    try:
        logger.log("job_start", job=JOB, date=run_date.isoformat(), force=args.force,
                   duration_minutes=args.duration_minutes,
                   contracts_limit=args.contracts_limit, underlying=args.underlying)
        market_gate.require_trading_day(run_date, force=args.force,
                                        data_root=settings.data_root)
        deadline = _window(run_date, settings, args.force, args.duration_minutes, logger)
        if deadline is None:
            settle(True, "capture window already closed for today")
            return 0
        underlyings = parse_underlyings(args.underlying, DEFAULT_UNDERLYINGS)
        try:
            tickers = select_tickers(
                settings, run_date, underlyings, args.contracts_limit, logger
            )
        except RuntimeError as exc:
            logger.log("job_error", job=JOB, error=str(exc))
            settle(False, f"no contract universe: {exc}")
            return 1
        chunks = subscribe_chunks(tickers)
        writer = HourlyJsonlWriter(settings.data_root, run_date, logger)
        writer.start()
        try:
            stats = asyncio.run(
                _capture(settings, logger, run_date, deadline, writer, chunks)
            )
        finally:
            writer.stop()
            writer.join(timeout=30)
        duration_s = round(time.monotonic() - started, 3)
        logger.log("job_end", job=JOB, rows=writer.rows_written, duration_s=duration_s,
                   **stats)
        # A capture window that ends with zero rows is a failure, not a
        # success: the subscription ACKed and nothing arrived.
        if writer.rows_written == 0:
            settle(False, f"captured 0 rows in {duration_s}s; stats={stats}")
        else:
            settle(True, f"ok: rows={writer.rows_written} in {duration_s}s; stats={stats}")
        return 0
    except _FatalAuth as exc:
        logger.log("job_error", job=JOB, error=str(exc))
        settle(False, f"auth failure: {exc}")
        return 1
    except KeyboardInterrupt:
        logger.log("job_interrupted", job=JOB)
        settle(True, "interrupted by operator")
        return 0
    except SystemExit as exc:
        # market_gate.require_trading_day() exits 0 on holidays.
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        settle(code == 0, f"exited {code} (not a trading day, or nothing to do)")
        raise
    except BaseException as exc:  # noqa: BLE001 - must not leave the check hung
        logger.log("job_error", job=JOB, error=f"{type(exc).__name__}: {exc}")
        settle(False, f"crashed: {type(exc).__name__}: {exc}")
        raise
    finally:
        logger.close()


if __name__ == "__main__":
    sys.exit(main())
