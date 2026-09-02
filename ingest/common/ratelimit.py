"""Process-wide token bucket for outbound REST calls.

The vendor publishes no ``X-RateLimit-*`` headers and the plan is nominally
unmetered, but the API *does* return 429 under burst. Measured 2026-08-31:
60 sequential requests completed in 12.9s (~4.6 req/s) with zero 429s, while
tight aggregate bursts did return 429.

``MassiveClient`` already retries 429 with exponential backoff
(``http_client.RETRYABLE_STATUS``); this bucket is what keeps it from getting
there once jobs run requests concurrently. It is deliberately a *shared*
limiter: the point is to bound total outbound rate, not per-thread rate.

"Shared" used to stop at the process boundary, which is not where the vendor's
429 counter stops. cron runs these jobs as separate processes and they overlap
constantly -- ``trades_watchlist`` occupies ~80% of every five-minute slot and
``snapshot_sweep`` fires straight through it -- so a 40 rps ceiling per process
was really an 80+ rps ceiling on the box, and the dataset that would pay for
the resulting throttling is ``option_snapshots``, the one that cannot be
re-pulled. :class:`SharedTokenBucket` puts the bucket in a small file under
``_meta/`` so every process on the box draws from the same tokens.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import threading
import time
from pathlib import Path

# Default ceiling. Well above the measured sequential 4.6 req/s so concurrency
# still buys throughput, but low enough to stay out of the burst-429 regime.
DEFAULT_RATE = float(os.environ.get("MASSIVE_MAX_RPS", "40"))
DEFAULT_BURST = float(os.environ.get("MASSIVE_BURST", "40"))

# Fraction of the bucket that low-priority callers may not touch.
#
# One shared allowance across the box is only half the answer: the jobs
# competing for it are not equal. option_snapshots cannot be backfilled --
# IV, greeks and open interest exist solely at the instant they are swept --
# while trades and bars can be re-pulled from S3 flat files years later. But
# trades_watchlist polls ~9,500 tickers and occupies roughly 80% of every
# five-minute slot, so on a first-come-first-served bucket the sweep queues
# behind the job whose data is replaceable.
#
# So low-priority callers stop drawing once the bucket falls to this reserve,
# leaving it for the sweep. The README states the rule ("everything else
# yields API budget to it"); this is where it is actually enforced.
RESERVE_FRACTION = float(os.environ.get("MASSIVE_PRIORITY_RESERVE", "0.35"))

LOW, NORMAL = "low", "normal"


class TokenBucket:
    """Thread-safe token bucket limiter.

    ``acquire()`` blocks until a token is available. Tokens refill
    continuously at ``rate`` per second up to ``burst``.
    """

    def __init__(self, rate: float = DEFAULT_RATE, burst: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.burst = burst if burst is not None else rate
        self._tokens = self.burst
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0, priority: str = NORMAL) -> float:
        """Block until ``tokens`` are available; return seconds spent waiting.

        ``priority=LOW`` callers stop drawing at :data:`RESERVE_FRACTION` of
        the burst, so the tokens a snapshot sweep needs are still there when
        it asks.
        """
        floor = self.burst * RESERVE_FRACTION if priority == LOW else 0.0
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.burst, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens - tokens >= floor:
                    self._tokens -= tokens
                    return waited
                deficit = tokens + floor - self._tokens
                sleep_s = deficit / self.rate
            time.sleep(sleep_s)
            waited += sleep_s


class SharedTokenBucket:
    """Token bucket whose state lives in a file, shared across processes.

    Same contract as :class:`TokenBucket`, but the tokens are held in a small
    JSON file guarded by ``flock``, so concurrent *processes* -- not just
    concurrent threads -- draw from one allowance.

    Degradation is deliberate: if the state file cannot be created or locked
    (read-only mount, exotic filesystem), this falls back to a private
    in-process bucket rather than failing the job. Rate limiting is a
    politeness mechanism, not a correctness one, and it must never be the
    reason a sweep does not happen.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        rate: float = DEFAULT_RATE,
        burst: float | None = None,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.burst = burst if burst is not None else rate
        self.path = Path(path)
        self._local = TokenBucket(rate, self.burst)
        self._degraded = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._degraded = True

    def _read(self, fh) -> tuple[float, float]:
        fh.seek(0)
        raw = fh.read()
        if not raw:
            return self.burst, time.time()
        try:
            state = json.loads(raw)
            return float(state["tokens"]), float(state["updated"])
        except (ValueError, KeyError, TypeError):
            return self.burst, time.time()

    def _write(self, fh, tokens: float, updated: float) -> None:
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"tokens": tokens, "updated": updated}))
        fh.flush()

    def acquire(self, tokens: float = 1.0, priority: str = NORMAL) -> float:
        """Block until ``tokens`` are available; return seconds spent waiting."""
        if self._degraded:
            return self._local.acquire(tokens, priority)
        floor = self.burst * RESERVE_FRACTION if priority == LOW else 0.0
        waited = 0.0
        while True:
            try:
                with open(self.path, "a+", encoding="utf-8") as fh:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                    try:
                        have, updated = self._read(fh)
                        now = time.time()
                        # A clock that jumped backwards must not mint tokens
                        # nor freeze the bucket forever.
                        elapsed = max(0.0, min(now - updated, self.burst / self.rate))
                        have = min(self.burst, have + elapsed * self.rate)
                        if have - tokens >= floor:
                            self._write(fh, have - tokens, now)
                            return waited
                        deficit = tokens + floor - have
                        self._write(fh, have, now)
                        sleep_s = deficit / self.rate
                    finally:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                if exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM, errno.ENOSYS):
                    self._degraded = True
                    return self._local.acquire(tokens, priority)
                raise
            # Slept outside the lock, so other processes make progress.
            time.sleep(sleep_s)
            waited += sleep_s


_default_lock = threading.Lock()
_default: TokenBucket | SharedTokenBucket | None = None

# Where the cross-process bucket keeps its tokens. Under DATA_ROOT so it lives
# beside the rest of the box's mutable state; MASSIVE_RATELIMIT_STATE overrides
# it, and MASSIVE_RATELIMIT_SHARED=0 opts back out to per-process behaviour.
DEFAULT_STATE_PATH = "_meta/ratelimit.json"


def _shared_enabled() -> bool:
    return os.environ.get("MASSIVE_RATELIMIT_SHARED", "1").strip().lower() not in (
        "0", "false", "no", ""
    )


def _state_path() -> Path:
    override = os.environ.get("MASSIVE_RATELIMIT_STATE")
    if override:
        return Path(override)
    data_root = os.environ.get("DATA_ROOT", "/data/massive")
    return Path(data_root) / DEFAULT_STATE_PATH


def default_bucket() -> TokenBucket | SharedTokenBucket:
    """Lazily-built bucket sized from ``MASSIVE_MAX_RPS``.

    Cross-process by default: every job on the box shares one allowance, which
    is what the vendor actually meters. Set ``MASSIVE_RATELIMIT_SHARED=0`` for
    the old per-process behaviour.
    """
    global _default
    with _default_lock:
        if _default is None:
            if _shared_enabled():
                _default = SharedTokenBucket(_state_path(), DEFAULT_RATE, DEFAULT_BURST)
            else:
                _default = TokenBucket(DEFAULT_RATE, DEFAULT_BURST)
        return _default


def reset_default_bucket() -> None:
    """Drop the memoised bucket (tests, and any process that re-reads config)."""
    global _default
    with _default_lock:
        _default = None
