"""Tests for per-job Healthchecks pings.

The behaviour that matters: every job gets its OWN check. A single shared
check goes green the moment any one job succeeds, which hides the failure mode
this repo actually suffers from — a job that quietly stops running.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from ingest.common import cli
from ingest.common.config import Settings


def _settings(tmp_path: Path, **kw) -> Settings:
    return dataclasses.replace(
        Settings(massive_api_key="k", data_root=tmp_path, log_root=tmp_path / "logs"),
        **kw,
    )


class _Recorder:
    """Captures ping calls instead of hitting the network."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.exc = exc

    def __call__(self, url, data=None, timeout=None):
        if self.exc:
            raise self.exc
        self.calls.append((url, data or b""))


@pytest.fixture()
def recorder(monkeypatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(cli.requests, "post", rec)
    return rec


# ---------------------------------------------------------------------------
# Slug / URL construction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("job", "slug"),
    [
        ("contracts_sync", "massive-contracts-sync"),
        ("snapshot_sweep", "massive-snapshot-sweep"),
        ("coverage_audit", "massive-coverage-audit"),
        ("ws_minute_bars", "massive-ws-minute-bars"),
    ],
)
def test_slug_is_per_job_and_hyphenated(job: str, slug: str) -> None:
    assert cli.healthcheck_slug(job) == slug


def test_every_job_gets_a_distinct_url(tmp_path: Path) -> None:
    settings = _settings(tmp_path, healthchecks_ping_key="KEY")
    jobs = ["contracts_sync", "snapshot_sweep", "trades_watchlist",
            "flatfile_pull", "reconcile", "coverage_audit", "ws_minute_bars"]
    urls = {cli.healthcheck_url(settings, j)[0] for j in jobs}
    assert len(urls) == len(jobs), "each job must map to its own check"


def test_ping_key_builds_autocreating_url(tmp_path: Path) -> None:
    settings = _settings(tmp_path, healthchecks_ping_key="KEY")
    url, autocreate = cli.healthcheck_url(settings, "snapshot_sweep")
    assert url == "https://hc-ping.com/KEY/massive-snapshot-sweep"
    assert autocreate is True


def test_shared_url_still_supported_but_not_autocreating(tmp_path: Path) -> None:
    settings = _settings(tmp_path, healthchecks_ping_url="https://hc-ping.com/UUID")
    url, autocreate = cli.healthcheck_url(settings, "snapshot_sweep")
    assert url == "https://hc-ping.com/UUID"
    assert autocreate is False


def test_ping_key_takes_precedence_over_shared_url(tmp_path: Path) -> None:
    settings = _settings(tmp_path, healthchecks_ping_key="KEY",
                         healthchecks_ping_url="https://hc-ping.com/UUID")
    url, _ = cli.healthcheck_url(settings, "reconcile")
    assert url.endswith("/massive-reconcile")


def test_no_config_means_no_url(tmp_path: Path) -> None:
    assert cli.healthcheck_url(_settings(tmp_path), "reconcile") == (None, False)


def test_custom_base_is_honoured(tmp_path: Path) -> None:
    """Self-hosted Healthchecks instances use a different host."""
    settings = _settings(tmp_path, healthchecks_ping_key="KEY",
                         healthchecks_base="https://hc.example.internal")
    url, _ = cli.healthcheck_url(settings, "reconcile")
    assert url == "https://hc.example.internal/KEY/massive-reconcile"


# ---------------------------------------------------------------------------
# Ping behaviour
# ---------------------------------------------------------------------------

def test_ping_appends_suffix_and_create_flag(recorder: _Recorder) -> None:
    cli.ping("https://hc-ping.com/KEY/massive-x", "/fail", autocreate=True, body="boom")
    (url, data), = recorder.calls
    assert url == "https://hc-ping.com/KEY/massive-x/fail?create=1"
    assert data == b"boom"


def test_ping_omits_create_flag_for_shared_url(recorder: _Recorder) -> None:
    cli.ping("https://hc-ping.com/UUID", "/start", autocreate=False)
    (url, _), = recorder.calls
    assert url == "https://hc-ping.com/UUID/start"


def test_ping_is_a_noop_without_a_url(recorder: _Recorder) -> None:
    cli.ping(None, "/fail", autocreate=True)
    assert recorder.calls == []


def test_ping_never_raises(monkeypatch, capsys) -> None:
    """Monitoring must not be able to fail the job it monitors."""
    monkeypatch.setattr(cli.requests, "post", _Recorder(exc=RuntimeError("network down")))
    cli.ping("https://hc-ping.com/KEY/massive-x", "/fail", autocreate=True)
    assert "healthcheck ping failed" in capsys.readouterr().err


def test_ping_body_is_truncated(recorder: _Recorder) -> None:
    cli.ping("https://hc-ping.com/KEY/x", body="A" * 50_000)
    (_url, data), = recorder.calls
    assert len(data) == 10_000


# ---------------------------------------------------------------------------
# Monitoring config must not drift from the schedule
# ---------------------------------------------------------------------------

def _setup_module():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "scripts" / "setup_healthchecks.py"
    spec = importlib.util.spec_from_file_location("setup_healthchecks", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _scheduled_jobs() -> set[str]:
    """Job modules actually scheduled in deploy/crontab."""
    import re
    crontab = (Path(__file__).resolve().parents[1] / "deploy" / "crontab").read_text()
    jobs = set()
    for line in crontab.splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            continue
        m = re.search(r"ingest\.jobs\.(\w+)", line)
        if m:
            jobs.add(m.group(1))
    return jobs


def test_setup_script_slugs_match_the_runtime() -> None:
    """A mismatch would silently create a second, never-pinged check."""
    mod = _setup_module()
    for job in mod.JOBS:
        assert mod.slug_for(job) == cli.healthcheck_slug(job)


def test_every_scheduled_job_is_monitored() -> None:
    """Adding a cron line without a check is how a job dies unnoticed."""
    mod = _setup_module()
    missing = _scheduled_jobs() - set(mod.JOBS)
    assert not missing, f"scheduled but unmonitored: {sorted(missing)}"


def test_no_monitoring_for_unscheduled_jobs() -> None:
    """A check for a job nothing runs would alert forever."""
    mod = _setup_module()
    extra = set(mod.JOBS) - _scheduled_jobs()
    assert not extra, f"monitored but not scheduled: {sorted(extra)}"


def test_eod_dayaggs_is_deliberately_unmonitored() -> None:
    mod = _setup_module()
    assert "eod_dayaggs_rest" not in mod.JOBS
