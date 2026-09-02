"""Tests for the shared token bucket that bounds outbound REST rate."""

from __future__ import annotations

import errno
import json
import threading
import time

import pytest

from ingest.common import ratelimit
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


# ---------------------------------------------------------------------------
# Cross-process sharing
# ---------------------------------------------------------------------------
#
# The per-process bucket bounded each job at MASSIVE_MAX_RPS independently.
# cron overlaps these jobs constantly, so the box's real outbound rate was a
# multiple of the configured ceiling -- and the vendor meters the box, not the
# process.

def test_shared_bucket_state_is_visible_to_another_instance(tmp_path) -> None:
    """Two bucket objects on one file draw from the same tokens."""
    path = tmp_path / "ratelimit.json"
    a = ratelimit.SharedTokenBucket(path, rate=1000.0, burst=10.0)
    b = ratelimit.SharedTokenBucket(path, rate=1000.0, burst=10.0)
    for _ in range(10):
        assert a.acquire() == 0.0
    # The allowance is gone; b must wait rather than getting its own ten.
    assert b.acquire() > 0.0


def test_shared_bucket_refills_over_time(tmp_path) -> None:
    path = tmp_path / "ratelimit.json"
    bucket = ratelimit.SharedTokenBucket(path, rate=100.0, burst=1.0)
    bucket.acquire()
    waited = bucket.acquire()
    assert 0.0 < waited < 0.5


def test_shared_bucket_survives_a_corrupt_state_file(tmp_path) -> None:
    """A truncated or hand-edited file must not wedge every job on the box."""
    path = tmp_path / "ratelimit.json"
    path.write_text("{not json", encoding="utf-8")
    bucket = ratelimit.SharedTokenBucket(path, rate=100.0, burst=5.0)
    assert bucket.acquire() == 0.0


def test_shared_bucket_ignores_a_backwards_clock(tmp_path) -> None:
    """A clock jump must neither mint tokens nor freeze the bucket."""
    import json as _json
    path = tmp_path / "ratelimit.json"
    bucket = ratelimit.SharedTokenBucket(path, rate=10.0, burst=10.0)
    bucket.acquire()
    path.write_text(_json.dumps({"tokens": 0.0, "updated": time.time() + 3600}),
                    encoding="utf-8")
    # Refill is clamped to at most a full burst's worth, never negative.
    assert bucket.acquire() >= 0.0


def test_default_bucket_is_shared_unless_opted_out(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MASSIVE_RATELIMIT_STATE", str(tmp_path / "rl.json"))
    monkeypatch.setenv("MASSIVE_RATELIMIT_SHARED", "1")
    ratelimit.reset_default_bucket()
    assert isinstance(ratelimit.default_bucket(), ratelimit.SharedTokenBucket)

    monkeypatch.setenv("MASSIVE_RATELIMIT_SHARED", "0")
    ratelimit.reset_default_bucket()
    assert isinstance(ratelimit.default_bucket(), ratelimit.TokenBucket)
    ratelimit.reset_default_bucket()


def test_shared_bucket_degrades_instead_of_failing(tmp_path, monkeypatch) -> None:
    """Rate limiting must never be the reason a sweep does not happen."""
    bucket = ratelimit.SharedTokenBucket(tmp_path / "rl.json", rate=100.0, burst=5.0)

    def _no_open(*a, **k):
        raise OSError(errno.EROFS, "read-only file system")

    monkeypatch.setattr("builtins.open", _no_open)
    assert bucket.acquire() == 0.0


# ---------------------------------------------------------------------------
# Priority: the irreplaceable dataset goes first
# ---------------------------------------------------------------------------

def test_low_priority_stops_at_the_reserve(tmp_path) -> None:
    """A low-priority caller must leave tokens for the snapshot sweep."""
    # Refill is slow enough to be negligible across the draws below, so the
    # count is deterministic rather than a race against the clock.
    bucket = ratelimit.SharedTokenBucket(tmp_path / "rl.json", rate=1.0, burst=10.0)
    reserve = 10.0 * ratelimit.RESERVE_FRACTION  # 3.5
    free = int(10.0 - reserve)                   # 6 immediate draws
    for _ in range(free):
        assert bucket.acquire(priority=ratelimit.LOW) == 0.0
    # The next one would eat into the reserve, so it has to wait for refill.
    assert bucket.acquire(priority=ratelimit.LOW) > 0.0


def test_normal_priority_may_drain_the_reserve(tmp_path) -> None:
    bucket = ratelimit.SharedTokenBucket(tmp_path / "rl.json", rate=1.0, burst=10.0)
    for _ in range(10):
        assert bucket.acquire(priority=ratelimit.NORMAL) == 0.0


def test_sweep_is_not_blocked_by_a_saturating_low_priority_job(tmp_path) -> None:
    """The whole point: trades polling must not starve snapshot_sweep.

    trades_watchlist polls ~9,500 tickers and runs for ~80% of every
    five-minute slot; the sweep fires straight through it every minute and
    cannot be re-run for that minute afterwards.
    """
    path = tmp_path / "rl.json"
    trades = ratelimit.SharedTokenBucket(path, rate=1.0, burst=10.0)
    sweep = ratelimit.SharedTokenBucket(path, rate=1.0, burst=10.0)
    for _ in range(int(10.0 - 10.0 * ratelimit.RESERVE_FRACTION)):
        assert trades.acquire(priority=ratelimit.LOW) == 0.0
    # trades has taken everything it is allowed to; the sweep still gets served
    # immediately out of the reserve.
    assert sweep.acquire(priority=ratelimit.NORMAL) == 0.0


def test_in_process_bucket_honours_priority_too(tmp_path) -> None:
    """MASSIVE_RATELIMIT_SHARED=0 must not silently drop the guarantee."""
    bucket = TokenBucket(rate=1.0, burst=10.0)
    for _ in range(int(10.0 - 10.0 * ratelimit.RESERVE_FRACTION)):
        assert bucket.acquire(priority=ratelimit.LOW) == 0.0
    assert bucket.acquire(priority=ratelimit.LOW) > 0.0


# ---------------------------------------------------------------------------
# Sustained priority, not just a first burst
# ---------------------------------------------------------------------------
#
# A reserve alone only protects the first burst: once a sweep has drained it,
# every later refill token is contended and a saturating low-priority job wins
# its share. snapshot_sweep paginates ~173 requests per run, far more than the
# ~14 tokens a reserve holds, so the guarantee has to survive the whole run.

def _saturate(bucket, stop: threading.Event, priority: str) -> None:
    while not stop.is_set():
        bucket.acquire(priority=priority)


@pytest.mark.parametrize("make", [
    lambda p: TokenBucket(rate=200.0, burst=10.0),
    lambda p: ratelimit.SharedTokenBucket(p, rate=200.0, burst=10.0),
])
def test_normal_keeps_its_rate_against_a_saturating_low_job(tmp_path, make) -> None:
    """A long normal-priority run must not be starved by low-priority load."""
    bucket = make(tmp_path / "rl.json")
    needed = 60  # a sweep's worth, well beyond the reserve

    stop = threading.Event()
    hogs = [
        threading.Thread(target=_saturate, args=(bucket, stop, ratelimit.LOW),
                         daemon=True)
        for _ in range(4)
    ]
    for h in hogs:
        h.start()
    time.sleep(0.1)  # let the hogs drain the bucket first

    try:
        start = time.monotonic()
        for _ in range(needed):
            bucket.acquire(priority=ratelimit.NORMAL)
        elapsed = time.monotonic() - start
    finally:
        stop.set()
        for h in hogs:
            h.join(timeout=2)

    # Unimpeded this is ~needed/rate = 0.3s. Without preemption four saturating
    # low-priority threads would take most of the refill and stretch it past a
    # second. The bound is loose enough for a busy box, tight enough to fail
    # if priority is not actually enforced.
    assert elapsed < 0.75, f"normal priority starved: {elapsed:.2f}s"


def test_low_priority_is_not_throttled_when_nothing_competes(tmp_path) -> None:
    """Yielding must cost nothing while the sweep is idle.

    The reserve plus claim is chosen over giving low priority its own smaller
    rate precisely so trades polling keeps the full budget outside the moments
    a sweep is actually waiting.
    """
    bucket = ratelimit.SharedTokenBucket(tmp_path / "rl.json", rate=500.0, burst=20.0)
    start = time.monotonic()
    for _ in range(100):
        bucket.acquire(priority=ratelimit.LOW)
    elapsed = time.monotonic() - start
    # 100 requests at 500/s from a 20-token burst is ~0.16s; the reserve must
    # not turn this into a multiple of that.
    assert elapsed < 0.5, f"low priority throttled with no contention: {elapsed:.2f}s"


def test_a_stale_claim_cannot_wedge_low_priority(tmp_path) -> None:
    """A job killed mid-acquire must not park the bucket indefinitely."""
    path = tmp_path / "rl.json"
    path.write_text(json.dumps({
        "tokens": 10.0, "updated": time.time(),
        "normal_claim": time.time() + 86_400,   # a claim from a bad clock
    }), encoding="utf-8")
    bucket = ratelimit.SharedTokenBucket(path, rate=100.0, burst=10.0)
    start = time.monotonic()
    bucket.acquire(priority=ratelimit.LOW)
    assert time.monotonic() - start < ratelimit.NORMAL_CLAIM_S + 1.0
