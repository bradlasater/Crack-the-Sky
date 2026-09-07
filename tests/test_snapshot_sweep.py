"""Per-chain failure isolation for the snapshot sweep.

The chains are independent and each writes its own parquet inside
``_sweep_underlying``, so one chain's error must not discard the chains that
already landed. It must also not propagate out of ``_main_fn``: ``run_job``
retries the whole sweep on a transient error, which would re-fetch the good
chains and file a *second* parquet for the same minute. Duplicate snapshot
rows are worse than a missed page here, because this is the one dataset that
cannot be backfilled -- there is no later pull to reconcile against.

The exception is a run where every chain fails, which is an outage rather than
a flaky chain, and has nothing landed for a retry to duplicate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from ingest.common import cli
from ingest.common.cli import _is_retryable
from ingest.common.config import Settings
from ingest.common.http_client import MassiveHTTPError
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import snapshot_sweep as job
from tests.conftest import load_fixture

RUN_DATE = "2026-09-04"
UNDERLYINGS = "SPY,I:SPX,VIX"


def _settings(data_root: Path, ping_key: str | None = None) -> Settings:
    return Settings(
        massive_api_key="test-key",
        data_root=data_root,
        log_root=data_root / "logs",
        healthchecks_ping_key=ping_key,
    )


def _args(**over) -> argparse.Namespace:
    base = {"date": RUN_DATE, "limit": None, "dry_run": False,
            "underlying": UNDERLYINGS}
    return argparse.Namespace(**{**base, **over})


def _results() -> list[dict]:
    return load_fixture("snapshot_options_spy.json")["results"]


def _client_factory(
    broken: set[str], fetched: list[str] | None = None, exc: Exception | None = None
):
    """A MassiveClient stand-in whose chains fail by name.

    ``exc`` chooses the failure type, which is what the retry-classification
    tests turn on; the default is a plain RuntimeError.
    """
    seen = fetched if fetched is not None else []

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        def get(self, path, params=None):
            # _sweep_underlying wraps this to count pages; paginate is stubbed
            # here, so a real call would mean the stub stopped being used.
            raise AssertionError("paginate is stubbed; get should not be called")

        def paginate(self, path, params=None, limit=250):
            underlying = path.rsplit("/", 1)[-1]
            seen.append(underlying)
            if underlying in broken:
                raise exc or RuntimeError(f"upstream exploded for {underlying}")
            return iter(_results())

    return _Client


def _run(tmp_path: Path, monkeypatch, broken: set[str], **over):
    fetched: list[str] = []
    monkeypatch.setattr(job, "MassiveClient", _client_factory(broken, fetched))
    settings = _settings(tmp_path)
    logger = JsonlLogger(path=tmp_path / "logs" / "sweep.jsonl", echo=False)
    try:
        summary = job._main_fn(_args(**over), settings, logger, False, False)
    finally:
        logger.close()
    return summary, fetched, tmp_path / "logs" / "sweep.jsonl"


def _landed(tmp_path: Path) -> list[str]:
    part = tmp_path / "clean" / "option_snapshots" / f"dt={RUN_DATE}"
    return sorted(p.name for p in part.iterdir()) if part.exists() else []


# ---------------------------------------------------------------------------
# Failure semantics
# ---------------------------------------------------------------------------

def test_every_chain_failing_raises(tmp_path: Path, monkeypatch) -> None:
    """A 100% failure rate is an outage and must not report success."""
    with pytest.raises(RuntimeError, match="upstream exploded"):
        _run(tmp_path, monkeypatch, {"SPY", "I:SPX", "VIX"})
    assert _landed(tmp_path) == []


def test_every_chain_failing_keeps_the_original_exception_retryable(
    tmp_path: Path, monkeypatch
) -> None:
    """run_job decides whether to retry from the exception *type*, and does not
    look through ``__cause__``. Summarising an exhausted 429/5xx into a
    RuntimeError would call a transient whole-endpoint outage deterministic and
    spend one attempt where three were available -- and this is the one path
    where retrying is free, since no chain landed anything to duplicate."""
    transient = MassiveHTTPError(503, "https://api.example/v3/snapshot/options/SPY")
    monkeypatch.setattr(
        job, "MassiveClient", _client_factory({"SPY", "I:SPX", "VIX"}, exc=transient)
    )
    logger = JsonlLogger(path=None, echo=False)
    with pytest.raises(MassiveHTTPError) as excinfo:
        job._main_fn(_args(), _settings(tmp_path), logger, False, False)
    assert excinfo.value.status_code == 503
    assert _is_retryable(excinfo.value) is True


def test_a_deterministic_all_chain_failure_stays_deterministic(
    tmp_path: Path, monkeypatch
) -> None:
    """The other half: a 403 must not become retryable on the way out."""
    denied = MassiveHTTPError(403, "https://api.example/v3/snapshot/options/SPY")
    monkeypatch.setattr(
        job, "MassiveClient", _client_factory({"SPY", "I:SPX", "VIX"}, exc=denied)
    )
    logger = JsonlLogger(path=None, echo=False)
    with pytest.raises(MassiveHTTPError) as excinfo:
        job._main_fn(_args(), _settings(tmp_path), logger, False, False)
    assert _is_retryable(excinfo.value) is False


def test_the_all_failed_summary_is_logged(tmp_path: Path, monkeypatch) -> None:
    """The count moved out of the exception message, so it must be in the log."""
    log_path = tmp_path / "logs" / "sweep.jsonl"
    logger = JsonlLogger(path=log_path, echo=False)
    monkeypatch.setattr(
        job, "MassiveClient", _client_factory({"SPY", "I:SPX", "VIX"})
    )
    with pytest.raises(RuntimeError):
        job._main_fn(_args(), _settings(tmp_path), logger, False, False)
    logger.close()
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    failed = [e for e in events if e["event"] == "sweep_failed"]
    assert len(failed) == 1
    assert failed[0]["chains"] == 3
    assert sorted(failed[0]["failed"]) == ["I:SPX", "SPY", "VIX"]


def test_one_bad_chain_still_lands_the_others(tmp_path: Path, monkeypatch) -> None:
    summary, _fetched, _log = _run(tmp_path, monkeypatch, {"VIX"})
    assert summary["errors"] == 1
    assert summary["rows"] > 0
    landed = _landed(tmp_path)
    assert len(landed) == 2, landed
    assert not any("VIX" in name for name in landed)


def test_a_partial_failure_does_not_re_fetch_the_good_chains(
    tmp_path: Path, monkeypatch
) -> None:
    """The duplicate-file path. Raising here would have run_job retry the whole
    sweep, re-fetching SPY and I:SPX into a second parquet for the same minute."""
    _summary, fetched, _log = _run(tmp_path, monkeypatch, {"VIX"})
    assert sorted(fetched) == ["I:SPX", "SPY", "VIX"]  # each chain fetched once
    assert len(_landed(tmp_path)) == 2  # and filed once


def test_the_failed_chain_is_named_in_the_log(tmp_path: Path, monkeypatch) -> None:
    """A partial failure returns success, so the log is the only place it shows."""
    _summary, _fetched, log_path = _run(tmp_path, monkeypatch, {"VIX"})
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    chain_errors = [e for e in events if e["event"] == "chain_error"]
    assert len(chain_errors) == 1
    assert chain_errors[0]["underlying"] == "VIX"
    assert "upstream exploded" in chain_errors[0]["error"]


def test_a_clean_run_reports_no_errors(tmp_path: Path, monkeypatch) -> None:
    summary, fetched, _log = _run(tmp_path, monkeypatch, set())
    assert summary["errors"] == 0
    assert sorted(fetched) == ["I:SPX", "SPY", "VIX"]
    assert len(_landed(tmp_path)) == 3


def test_no_underlyings_is_not_an_every_chain_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """``len(errors) == len(underlyings)`` is 0 == 0 with nothing to sweep; the
    guard must not index into an empty error list."""
    summary, fetched, _log = _run(tmp_path, monkeypatch, set(), underlying=",")
    assert summary["errors"] == 0
    assert summary["rows"] == 0
    assert fetched == []


# ---------------------------------------------------------------------------
# Degraded-capture alerting
#
# A partial failure has to report success, so the job's own check stays green
# even for a chain that is down on every run. The second check closes that:
# it is pinged only when every chain came back clean, which makes its grace
# window the thing that decides how long a chain may be down before it pages.
# Alerting by absence rather than by /fail is deliberate -- at a 1-minute
# cadence, failing on any bad chain would page on every transient 429.
# ---------------------------------------------------------------------------

ALL_CHAINS_SLUG = "massive-snapshot-sweep-all-chains"


class _PingRecorder:
    """Captures healthcheck pings instead of hitting the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    def __call__(self, url, data=None, timeout=None):
        self.calls.append((url, data or b""))

    def slugs(self) -> list[str]:
        return [url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
                for url, _ in self.calls]


def _run_with_pings(tmp_path: Path, monkeypatch, broken: set[str], **over):
    rec = _PingRecorder()
    monkeypatch.setattr(cli.requests, "post", rec)
    monkeypatch.setattr(job, "MassiveClient", _client_factory(broken))
    settings = _settings(tmp_path, ping_key="test-ping-key")
    logger = JsonlLogger(path=None, echo=False)
    summary = job._main_fn(_args(**over), settings, logger, False, False)
    return summary, rec


def test_a_clean_sweep_pings_the_all_chains_check(
    tmp_path: Path, monkeypatch
) -> None:
    _summary, rec = _run_with_pings(tmp_path, monkeypatch, set())
    assert rec.slugs() == [ALL_CHAINS_SLUG]
    assert b"3 chains clean" in rec.calls[0][1]


def test_a_partial_failure_withholds_the_all_chains_ping(
    tmp_path: Path, monkeypatch
) -> None:
    """The whole point: the run still succeeds, but the check goes unpinged, so
    a chain that stays down eventually breaches the grace and alerts."""
    summary, rec = _run_with_pings(tmp_path, monkeypatch, {"VIX"})
    assert summary["errors"] == 1  # the run itself reports success
    assert rec.slugs() == []  # and the degraded-capture check hears nothing


def test_a_dry_run_does_not_ping_the_all_chains_check(
    tmp_path: Path, monkeypatch
) -> None:
    """A dry run lands nothing, so it cannot attest the capture is healthy."""
    _summary, rec = _run_with_pings(tmp_path, monkeypatch, set(), dry_run=True)
    assert rec.slugs() == []


def test_a_sweep_with_no_underlyings_does_not_ping(
    tmp_path: Path, monkeypatch
) -> None:
    """Zero chains is not zero failures -- "all clean" would be a lie that
    resets the grace window and masks a real outage for its duration."""
    _summary, rec = _run_with_pings(tmp_path, monkeypatch, set(), underlying=",")
    assert rec.slugs() == []
