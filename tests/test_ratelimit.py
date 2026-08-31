"""Tests for the shared token bucket that bounds outbound REST rate."""

from __future__ import annotations

import threading
import time

from ingest.common.ratelimit import TokenBucket, default_bucket


def test_burst_is_served_immediately() -> None:
    bucket = TokenBucket(rate=10, burst=5)
    start = time.monotonic()
    for _ in range(5):
        bucket.acquire()
    assert time.monotonic() - start < 0.05


def test_sustained_rate_is_bounded() -> None:
    bucket = TokenBucket(rate=50, burst=1)
    start = time.monotonic()
    for _ in range(6):
        bucket.acquire()
    # 1 burst token + 5 refills at 50/s = ~0.10s.
    assert time.monotonic() - start >= 0.08


def test_concurrent_acquire_does_not_exceed_budget() -> None:
    """The point of the bucket: N threads share one budget, not N budgets."""
    bucket = TokenBucket(rate=100, burst=1)
    start = time.monotonic()

    def worker() -> None:
        for _ in range(5):
            bucket.acquire()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 20 tokens, 1 free, 19 at 100/s => >= ~0.19s regardless of thread count.
    assert time.monotonic() - start >= 0.15


def test_default_bucket_is_shared() -> None:
    assert default_bucket() is default_bucket()
