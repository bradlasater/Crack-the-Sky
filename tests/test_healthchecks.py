"""Tests for per-job Healthchecks pings.

The behaviour that matters: every job gets its OWN check. A single shared
check goes green the moment any one job succeeds, which hides the failure mode
this repo actually suffers from — a job that quietly stops running.
"""

from __future__ import annotations

import dataclasses
import sys
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


# ---------------------------------------------------------------------------
# Terminal pings: a run that starts must always finish the check
# ---------------------------------------------------------------------------

def _suffixes(rec: _Recorder) -> list[str]:
    """Ping suffixes in order, e.g. ['/start', '/fail']."""
    out = []
    for url, _ in rec.calls:
        tail = url.split("massive-", 1)[-1].split("?", 1)[0]
        out.append("/" + tail.split("/", 1)[1] if "/" in tail else "")
    return out


def test_holiday_exit_still_sends_a_terminal_ping(tmp_path, monkeypatch, recorder):
    """A market holiday must not leave the check hung.

    cron fires on weekdays regardless of the market calendar, so
    require_trading_day() raises SystemExit(0) roughly ten times a year. Without
    a terminal ping the check sits in 'started' and Healthchecks pages a hung
    run -- which is how alerting gets muted and stops working.
    """
    settings = _settings(tmp_path, healthchecks_ping_key="KEY")
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr(cli.market_gate, "require_trading_day",
                        lambda *a, **k: sys.exit(0))

    with pytest.raises(SystemExit) as excinfo:
        cli.run_job("contracts_sync", lambda a, s, log: {"rows": 0}, [])
    assert excinfo.value.code == 0
    assert _suffixes(recorder) == ["/start", ""], "expected start then success"


def test_nonzero_systemexit_pings_fail(tmp_path, monkeypatch, recorder):
    settings = _settings(tmp_path, healthchecks_ping_key="KEY")
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: settings))

    def boom(a, s, log):
        sys.exit(3)

    with pytest.raises(SystemExit):
        cli.run_job("contracts_sync", boom, [])
    assert _suffixes(recorder) == ["/start", "/fail"]


def test_success_pings_start_then_success(tmp_path, monkeypatch, recorder):
    settings = _settings(tmp_path, healthchecks_ping_key="KEY")
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: settings))
    with pytest.raises(SystemExit) as excinfo:
        cli.run_job("contracts_sync", lambda a, s, log: {"rows": 7}, [])
    assert excinfo.value.code == 0
    assert _suffixes(recorder) == ["/start", ""]


def test_exception_pings_start_then_fail(tmp_path, monkeypatch, recorder):
    settings = _settings(tmp_path, healthchecks_ping_key="KEY")
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: settings))

    def boom(a, s, log):
        raise RuntimeError("upstream exploded")

    with pytest.raises(SystemExit):
        cli.run_job("contracts_sync", boom, [])
    assert _suffixes(recorder) == ["/start", "/fail"]


def test_every_run_job_path_sends_exactly_one_terminal_ping(tmp_path, monkeypatch, recorder):
    """No path may send two terminal pings, or none."""
    settings = _settings(tmp_path, healthchecks_ping_key="KEY")
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: settings))
    cases = [
        lambda a, s, log: {"rows": 1},
        lambda a, s, log: sys.exit(0),
        lambda a, s, log: sys.exit(2),
    ]
    for fn in cases:
        recorder.calls.clear()
        with pytest.raises(SystemExit):
            cli.run_job("contracts_sync", fn, [])
        suf = _suffixes(recorder)
        assert suf[0] == "/start"
        assert len(suf) == 2, f"expected one terminal ping, got {suf}"


# ---------------------------------------------------------------------------
# Self-hosted routing
# ---------------------------------------------------------------------------

def test_self_hosted_base_is_the_ping_root(tmp_path) -> None:
    """Self-hosted Healthchecks serves pings under /ping, not the site root."""
    settings = _settings(tmp_path, healthchecks_ping_key="KEY",
                         healthchecks_base="https://hc.example.internal/ping")
    url, _ = cli.healthcheck_url(settings, "reconcile")
    assert url == "https://hc.example.internal/ping/KEY/massive-reconcile"


def test_setup_script_accepts_a_custom_api_base() -> None:
    """A self-hosted instance must be able to create its own checks."""
    mod = _setup_module()
    parsed = mod.argparse.ArgumentParser  # sanity: argparse is imported
    assert parsed is not None
    assert mod.DEFAULT_API_BASE == "https://healthchecks.io/api/v3"
    import inspect
    assert "api_base" in inspect.signature(mod._request).parameters


def test_keyboard_interrupt_still_settles_the_check(tmp_path, monkeypatch, recorder):
    """KeyboardInterrupt is a BaseException and skipped both handlers.

    A run interrupted at the console otherwise left the check 'started' until
    Healthchecks reported a hung run.
    """
    settings = _settings(tmp_path, healthchecks_ping_key="KEY")
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: settings))

    def interrupted(a, s, log):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cli.run_job("contracts_sync", interrupted, [])
    assert _suffixes(recorder) == ["/start", "/fail"]


def test_base_exception_still_settles_the_check(tmp_path, monkeypatch, recorder):
    settings = _settings(tmp_path, healthchecks_ping_key="KEY")
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: settings))

    class Weird(BaseException):
        pass

    def boom(a, s, log):
        raise Weird("not an Exception subclass")

    with pytest.raises(SystemExit):
        cli.run_job("contracts_sync", boom, [])
    assert _suffixes(recorder) == ["/start", "/fail"]


@pytest.mark.parametrize(
    "outcome",
    ["ok", "sysexit0", "sysexit2", "exception", "keyboard-interrupt"],
)
def test_terminal_ping_matrix(outcome, tmp_path, monkeypatch, recorder):
    """Every documented exit path sends exactly one terminal ping."""
    settings = _settings(tmp_path, healthchecks_ping_key="KEY")
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: settings))
    fns = {
        "ok": lambda a, s, log: {"rows": 1},
        "sysexit0": lambda a, s, log: sys.exit(0),
        "sysexit2": lambda a, s, log: sys.exit(2),
        "exception": lambda a, s, log: (_ for _ in ()).throw(RuntimeError("x")),
        "keyboard-interrupt": lambda a, s, log: (_ for _ in ()).throw(KeyboardInterrupt()),
    }
    with pytest.raises(BaseException):  # noqa: B017 - SystemExit or KeyboardInterrupt
        cli.run_job("contracts_sync", fns[outcome], [])
    suf = _suffixes(recorder)
    assert suf[0] == "/start"
    assert len(suf) == 2, f"{outcome}: expected one terminal ping, got {suf}"
