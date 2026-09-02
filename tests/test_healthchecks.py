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


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch) -> None:
    """Retry backoff is real in prod (30s+); tests must not wait for it."""
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)


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


def test_no_config_means_no_url(tmp_path: Path) -> None:
    assert cli.healthcheck_url(_settings(tmp_path), "reconcile") == (None, False)


def test_legacy_ping_url_without_key_fails_at_load(tmp_path: Path, monkeypatch, capsys) -> None:
    """Leftover HEALTHCHECKS_PING_URL must not silently disable monitoring."""
    env_file = tmp_path / ".env"
    env_file.write_text("MASSIVE_API_KEY=k\n")
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("HEALTHCHECKS_PING_URL", "https://hc-ping.com/UUID")
    monkeypatch.delenv("HEALTHCHECKS_PING_KEY", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        Settings.load(env_file)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "HEALTHCHECKS_PING_URL is no longer supported" in err
    assert "HEALTHCHECKS_PING_KEY" in err


def test_legacy_ping_url_ignored_when_ping_key_is_set(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MASSIVE_API_KEY=k\n")
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("HEALTHCHECKS_PING_URL", "https://hc-ping.com/UUID")
    monkeypatch.setenv("HEALTHCHECKS_PING_KEY", "KEY")
    settings = Settings.load(env_file)
    assert settings.healthchecks_ping_key == "KEY"


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


def test_ping_omits_create_flag_when_not_autocreating(recorder: _Recorder) -> None:
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
        m = re.search(r"(?:ingest\.jobs|pricing)\.(\w+)", line)
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


# Checks that are pinged by a running job rather than started by cron. Each
# needs an owning cron job, or it is exactly the never-pinged check the test
# above exists to prevent.
JOB_PINGED_CHECKS = {"ws_minute_bars_alive": "ws_minute_bars"}


def test_no_monitoring_for_unscheduled_jobs() -> None:
    """A check for a job nothing runs would alert forever."""
    mod = _setup_module()
    extra = set(mod.JOBS) - _scheduled_jobs() - set(JOB_PINGED_CHECKS)
    assert not extra, f"monitored but not scheduled: {sorted(extra)}"


def test_job_pinged_checks_have_a_scheduled_owner() -> None:
    """A liveness check is only meaningful while its owning job is running."""
    mod = _setup_module()
    scheduled = _scheduled_jobs()
    for check, owner in JOB_PINGED_CHECKS.items():
        assert check in mod.JOBS, f"{check} is not registered"
        assert owner in scheduled, f"{check} has no scheduled owner ({owner})"


def test_liveness_check_slug_matches_what_the_job_pings() -> None:
    """The job and the setup script must agree, or the check is never pinged."""
    from ingest.jobs.ws_minute_bars import LIVENESS_JOB

    mod = _setup_module()
    assert LIVENESS_JOB in mod.JOBS
    assert mod.slug_for(LIVENESS_JOB) == cli.healthcheck_slug(LIVENESS_JOB)


def test_eod_dayaggs_is_deliberately_unmonitored() -> None:
    mod = _setup_module()
    assert "eod_dayaggs_rest" not in mod.JOBS


# ---------------------------------------------------------------------------
# Setup script credential resolution (management API key)
# ---------------------------------------------------------------------------

def test_environment_wins_over_env_file(tmp_path, monkeypatch) -> None:
    mod = _setup_module()
    env_file = tmp_path / ".env"
    env_file.write_text(f"{mod.API_KEY_ENV}=from-file\n")
    monkeypatch.setenv(mod.API_KEY_ENV, "from-environment")
    assert mod.api_key_from_env(env_file) == "from-environment"


def test_blank_environment_value_falls_back_to_env_file(tmp_path, monkeypatch) -> None:
    mod = _setup_module()
    env_file = tmp_path / ".env"
    env_file.write_text(f"{mod.API_KEY_ENV}=from-file\n")
    monkeypatch.setenv(mod.API_KEY_ENV, "   ")
    assert mod.api_key_from_env(env_file) == "from-file"


def test_default_env_file_is_at_the_repo_root(tmp_path, monkeypatch) -> None:
    """With no explicit path, the key is read from <repo root>/.env."""
    mod = _setup_module()
    fake_script = tmp_path / "scripts" / "setup_healthchecks.py"
    fake_script.parent.mkdir()
    fake_script.touch()
    (tmp_path / ".env").write_text(f"{mod.API_KEY_ENV}=root-key\n")
    monkeypatch.setattr(mod, "__file__", str(fake_script))
    monkeypatch.delenv(mod.API_KEY_ENV, raising=False)
    assert mod.api_key_from_env() == "root-key"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('HEALTHCHECKS_API_KEY="quoted"', "quoted"),
        ("HEALTHCHECKS_API_KEY='single'", "single"),
        ("HEALTHCHECKS_API_KEY=  padded  ", "padded"),
        ("HEALTHCHECKS_API_KEY=", None),
        ('HEALTHCHECKS_API_KEY=""', None),
    ],
)
def test_env_file_values_are_unquoted_and_stripped(
    line: str, expected: str | None, tmp_path, monkeypatch
) -> None:
    mod = _setup_module()
    env_file = tmp_path / ".env"
    env_file.write_text(f"# a comment\n\nOTHER=1\n{line}\n")
    monkeypatch.delenv(mod.API_KEY_ENV, raising=False)
    assert mod.api_key_from_env(env_file) == expected


def test_missing_env_file_yields_no_key(tmp_path, monkeypatch) -> None:
    mod = _setup_module()
    monkeypatch.delenv(mod.API_KEY_ENV, raising=False)
    assert mod.api_key_from_env(tmp_path / "does-not-exist.env") is None


def test_cli_api_key_overrides_environment(monkeypatch) -> None:
    mod = _setup_module()
    monkeypatch.setenv(mod.API_KEY_ENV, "env-key")
    seen: list[str] = []

    def fake_request(method, path, api_key, payload=None, api_base=mod.DEFAULT_API_BASE):
        seen.append(api_key)
        return {"checks": []}

    monkeypatch.setattr(mod, "_request", fake_request)
    assert mod.main(["--api-key", "cli-key", "--dry-run"]) == 0
    assert seen and all(k == "cli-key" for k in seen)


def test_missing_key_exits_with_usage_error(monkeypatch, capsys) -> None:
    """No key anywhere must fail loudly, not fall through to the API."""
    mod = _setup_module()
    monkeypatch.delenv(mod.API_KEY_ENV, raising=False)
    monkeypatch.setattr(mod, "api_key_from_env", lambda env_path=None: None)
    with pytest.raises(SystemExit) as excinfo:
        mod.main(["--dry-run"])
    assert excinfo.value.code == 2
    assert "no management API key" in capsys.readouterr().err


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


# ---------------------------------------------------------------------------
# In-run retry: cron has no backoff, so transient failures retry in-process
# ---------------------------------------------------------------------------

def _run_settings(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path, healthchecks_ping_key="KEY")
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: settings))


def test_transient_failure_is_retried_then_succeeds(tmp_path, monkeypatch, recorder):
    """A one-off blip must not burn a whole cron tick."""
    _run_settings(monkeypatch, tmp_path)
    calls = {"n": 0}

    def flaky(a, s, log):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("blip")
        return {"rows": 5}

    with pytest.raises(SystemExit) as excinfo:
        cli.run_job("contracts_sync", flaky, [])
    assert excinfo.value.code == 0
    assert calls["n"] == 3
    assert _suffixes(recorder) == ["/start", ""], "retries share one run: no extra pings"


def test_retry_exhaustion_fails_with_a_single_terminal_ping(tmp_path, monkeypatch, recorder):
    _run_settings(monkeypatch, tmp_path)
    calls = {"n": 0}

    def always_broken(a, s, log):
        calls["n"] += 1
        raise ConnectionError("still down")

    with pytest.raises(SystemExit) as excinfo:
        cli.run_job("contracts_sync", always_broken, [])
    assert excinfo.value.code == 1
    assert calls["n"] == 3, "default JOB_MAX_ATTEMPTS is 3"
    assert _suffixes(recorder) == ["/start", "/fail"]


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("bad schema"),
        PermissionError("HTTP 403: not entitled"),
        cli.MassiveHTTPError(400, "https://api.polygon.io/v3/x"),
        RuntimeError("bug"),
    ],
)
def test_deterministic_failures_are_not_retried(tmp_path, monkeypatch, recorder, exc):
    """403s, 4xx, schema/argument errors and bugs rerun the job for nothing."""
    _run_settings(monkeypatch, tmp_path)
    calls = {"n": 0}

    def boom(a, s, log):
        calls["n"] += 1
        raise exc

    with pytest.raises(SystemExit) as excinfo:
        cli.run_job("contracts_sync", boom, [])
    assert excinfo.value.code == 1
    assert calls["n"] == 1
    assert _suffixes(recorder) == ["/start", "/fail"]


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("reset"),
        TimeoutError("ws read deadline"),
        cli.MassiveHTTPError(429, "https://api.polygon.io/v3/x"),
        cli.MassiveHTTPError(503, "https://api.polygon.io/v3/x"),
    ],
)
def test_transient_failures_are_retried(tmp_path, monkeypatch, recorder, exc):
    _run_settings(monkeypatch, tmp_path)
    calls = {"n": 0}

    def flaky(a, s, log):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        return {"rows": 1}

    with pytest.raises(SystemExit) as excinfo:
        cli.run_job("contracts_sync", flaky, [])
    assert excinfo.value.code == 0
    assert calls["n"] == 2


def test_systemexit_is_never_retried(tmp_path, monkeypatch, recorder):
    """Deterministic exits (market gate, explicit sys.exit) fail immediately."""
    _run_settings(monkeypatch, tmp_path)
    calls = {"n": 0}

    def explicit_exit(a, s, log):
        calls["n"] += 1
        sys.exit(2)

    with pytest.raises(SystemExit):
        cli.run_job("contracts_sync", explicit_exit, [])
    assert calls["n"] == 1
    assert _suffixes(recorder) == ["/start", "/fail"]


def test_keyboard_interrupt_is_never_retried(tmp_path, monkeypatch, recorder):
    _run_settings(monkeypatch, tmp_path)
    calls = {"n": 0}

    def interrupted(a, s, log):
        calls["n"] += 1
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cli.run_job("contracts_sync", interrupted, [])
    assert calls["n"] == 1


def test_retry_attempts_are_env_configurable(tmp_path, monkeypatch, recorder):
    _run_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("JOB_MAX_ATTEMPTS", "1")
    calls = {"n": 0}

    def boom(a, s, log):
        calls["n"] += 1
        raise ConnectionError("x")

    with pytest.raises(SystemExit):
        cli.run_job("contracts_sync", boom, [])
    assert calls["n"] == 1, "JOB_MAX_ATTEMPTS=1 disables retry"


def test_bad_retry_config_crashes_before_the_check_starts(tmp_path, monkeypatch, recorder):
    """A typo like JOB_MAX_ATTEMPTS=three must not strand the check hung."""
    _run_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("JOB_MAX_ATTEMPTS", "three")
    with pytest.raises(ValueError):
        cli.run_job("contracts_sync", lambda a, s, log: {"rows": 1}, [])
    assert recorder.calls == [], "no /start may precede a config crash"


def test_retry_backoff_is_exponential_and_capped(tmp_path, monkeypatch, recorder):
    """Backoff doubles from JOB_RETRY_BASE_S and is capped at RETRY_CAP_S."""
    _run_settings(monkeypatch, tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)
    monkeypatch.setenv("JOB_MAX_ATTEMPTS", "7")
    monkeypatch.setenv("JOB_RETRY_BASE_S", "30")

    def boom(a, s, log):
        raise ConnectionError("x")

    with pytest.raises(SystemExit):
        cli.run_job("contracts_sync", boom, [])
    assert sleeps == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]


def test_liveness_schedule_stays_inside_the_capture_window() -> None:
    """Expecting a ping when the job is not running would alarm every day.

    The check exists to catch a mid-session death, so every minute it expects
    a ping must fall between the first stats tick and the capture deadline.
    A wider expression would be "better coverage" that pages nightly and gets
    muted, which is worse than the gap it closes.
    """
    from datetime import date, datetime, timedelta

    from ingest.common import market_gate
    from ingest.jobs.ws_minute_bars import STATS_INTERVAL_S, WINDOW_START

    mod = _setup_module()
    schedule, grace_minutes, _ = mod.JOBS["ws_minute_bars_alive"]
    minute_field, hour_field = schedule.split()[0], schedule.split()[1]

    def _expand(field: str, hi: int) -> list[int]:
        out: set[int] = set()
        for part in field.split(","):
            step = 1
            if "/" in part:
                part, step_s = part.split("/")
                step = int(step_s)
            if part == "*":
                lo_v, hi_v = 0, hi
            elif "-" in part:
                lo_s, hi_s = part.split("-")
                lo_v, hi_v = int(lo_s), int(hi_s)
            else:
                lo_v = hi_v = int(part)
            out.update(range(lo_v, hi_v + 1, step))
        return sorted(out)

    minutes, hours = _expand(minute_field, 59), _expand(hour_field, 23)
    first = datetime(2026, 9, 2, hours[0], minutes[0], tzinfo=market_gate.ET)
    last = datetime(2026, 9, 2, hours[-1], minutes[-1], tzinfo=market_gate.ET)

    day = date(2026, 9, 2)
    earliest_ping = (
        datetime.combine(day, WINDOW_START, tzinfo=market_gate.ET)
        + timedelta(seconds=STATS_INTERVAL_S)
    )
    deadline = market_gate.option_capture_end_et(day)

    assert first >= earliest_ping, (
        f"expects a ping at {first:%H:%M}, before the first stats tick "
        f"at {earliest_ping:%H:%M}"
    )
    assert last + timedelta(minutes=grace_minutes) <= deadline, (
        f"last expected ping {last:%H:%M} + {grace_minutes}m grace runs past "
        f"the {deadline:%H:%M} capture deadline"
    )


def test_run_check_grace_covers_the_whole_capture_window() -> None:
    """The terminal ping arrives hours after the scheduled start time."""
    from datetime import date, datetime, timedelta

    from ingest.common import market_gate

    mod = _setup_module()
    schedule, grace_minutes, _ = mod.JOBS["ws_minute_bars"]
    minute, hour = int(schedule.split()[0]), int(schedule.split()[1])

    day = date(2026, 9, 2)
    starts = datetime(2026, 9, 2, hour, minute, tzinfo=market_gate.ET)
    deadline = market_gate.option_capture_end_et(day)
    assert starts + timedelta(minutes=grace_minutes) > deadline, (
        "grace expires before the job's own terminal ping is due"
    )
