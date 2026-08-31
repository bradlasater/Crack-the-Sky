"""Process-wide token bucket for outbound REST calls.

The vendor publishes no ``X-RateLimit-*`` headers and the plan is nominally
unmetered, but the API *does* return 429 under burst. Measured 2026-08-31:
60 sequential requests completed in 12.9s (~4.6 req/s) with zero 429s, while
tight aggregate bursts did return 429.

``MassiveClient`` already retries 429 with exponential backoff
(``http_client.RETRYABLE_STATUS``); this bucket is what keeps it from getting
there once jobs run requests concurrently. It is deliberately a *shared*
limiter: the point is to bound total outbound rate across every worker thread
in a process, not per-thread rate.
"""

from __future__ import annotations

import os
import threading
import time

# Default ceiling. Well above the measured sequential 4.6 req/s so concurrency
# still buys throughput, but low enough to stay out of the burst-429 regime.
DEFAULT_RATE = float(os.environ.get("MASSIVE_MAX_RPS", "40"))
DEFAULT_BURST = float(os.environ.get("MASSIVE_BURST", "40"))


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

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until ``tokens`` are available; return seconds spent waiting."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.burst, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                sleep_s = deficit / self.rate
            time.sleep(sleep_s)
            waited += sleep_s


_default_lock = threading.Lock()
_default: TokenBucket | None = None


def default_bucket() -> TokenBucket:
    """Lazily-built process-wide bucket sized from ``MASSIVE_MAX_RPS``."""
    global _default
    with _default_lock:
        if _default is None:
            _default = TokenBucket(DEFAULT_RATE, DEFAULT_BURST)
        return _default
