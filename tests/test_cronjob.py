"""scripts/cronjob.sh: lock skip vs the wrapped command's own exit status.

The wrapper used to run `flock -n -E 99` around the command, so a command
that exited 99 was logged as job_skipped and swallowed to 0. The lock is
now taken on fd 9 before the command runs: contention is flock's `-E 99`
on that fd, other flock errors stay nonzero, and the command's own 99 is
preserved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CRONJOB = REPO / "scripts" / "cronjob.sh"


def _job_name() -> str:
    return "cjtest_" + uuid.uuid4().hex[:12]


def _run(
    job: str, *command: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(CRONJOB), job, *command],
        capture_output=True,
        text=True,
        env=merged,
        timeout=30,
    )


def test_command_exit_99_is_not_a_skip() -> None:
    job = _job_name()
    result = _run(job, "bash", "-c", "exit 99")
    assert result.returncode == 99
    assert "job_skipped" not in result.stdout


def test_command_exit_1_is_preserved() -> None:
    job = _job_name()
    result = _run(job, "bash", "-c", "exit 1")
    assert result.returncode == 1
    assert "job_skipped" not in result.stdout


def test_command_exit_0_is_success() -> None:
    job = _job_name()
    result = _run(job, "true")
    assert result.returncode == 0
    assert "job_skipped" not in result.stdout


def test_lock_open_failure_is_not_a_skip() -> None:
    job = _job_name()
    lock = Path(f"/tmp/massive-{job}.lock")
    lock.mkdir()
    try:
        result = _run(job, "true")
        assert result.returncode != 0
        assert "job_skipped" not in result.stdout
        assert "cannot open lock" in result.stderr
    finally:
        lock.rmdir()


def test_flock_operational_error_is_not_a_skip(tmp_path: Path) -> None:
    job = _job_name()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    flock = bin_dir / "flock"
    flock.write_text("#!/bin/bash\nexit 2\n")
    flock.chmod(0o755)
    result = _run(job, "true", env={"PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert result.returncode == 2
    assert "job_skipped" not in result.stdout
    assert "flock failed with status 2" in result.stderr


def test_lock_held_logs_skip_and_exits_0() -> None:
    job = _job_name()
    lock = f"/tmp/massive-{job}.lock"
    holder = subprocess.Popen(
        ["bash", "-c", f"exec 9>'{lock}'; flock 9; printf 'HELD\\n'; sleep 60"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        line = holder.stdout.readline()
        assert line.strip() == "HELD", f"lock holder failed: {line!r}"
        result = _run(job, "true")
        assert result.returncode == 0
        assert '"event":"job_skipped"' in result.stdout
        assert job in result.stdout
    finally:
        holder.kill()
        holder.wait(timeout=5)


def _fake_curl(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "curl.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(f"#!/bin/bash\nprintf '%s\\n' \"$*\" >> '{log}'\nexit 0\n")
    curl.chmod(0o755)
    return str(bin_dir), log


def _sh(tmp_path: Path, rc: int) -> Path:
    script = tmp_path / "job.sh"
    script.write_text(f"#!/bin/bash\nexit {rc}\n")
    script.chmod(0o755)
    return script


def test_bash_script_job_pings_start_then_success(tmp_path: Path) -> None:
    job = _job_name()
    bin_dir, log = _fake_curl(tmp_path)
    script = _sh(tmp_path, 0)
    result = _run(
        job,
        "bash",
        str(script),
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HEALTHCHECKS_PING_KEY": "KEY",
            "HEALTHCHECKS_BASE": "https://hc.example.internal/ping",
        },
    )
    assert result.returncode == 0
    lines = log.read_text().splitlines()
    assert len(lines) == 2, lines
    slug = "massive-" + job.replace("_", "-")
    assert f"https://hc.example.internal/ping/KEY/{slug}/start?create=1" in lines[0]
    assert f"https://hc.example.internal/ping/KEY/{slug}?create=1" in lines[1]


def test_bash_script_job_pings_fail_on_nonzero(tmp_path: Path) -> None:
    job = _job_name()
    bin_dir, log = _fake_curl(tmp_path)
    script = _sh(tmp_path, 3)
    result = _run(
        job,
        "bash",
        str(script),
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HEALTHCHECKS_PING_KEY": "KEY",
            "HEALTHCHECKS_BASE": "https://hc.example.internal/ping",
        },
    )
    assert result.returncode == 3
    lines = log.read_text().splitlines()
    assert len(lines) == 2, lines
    slug = "massive-" + job.replace("_", "-")
    assert f"/{slug}/start?create=1" in lines[0]
    assert f"/{slug}/fail?create=1" in lines[1]


def test_crontab_loads_healthchecks_from_dotenv(tmp_path: Path) -> None:
    """Crontab does not source .env; a copied wrapper must read it itself."""
    job = _job_name()
    bin_dir, log = _fake_curl(tmp_path)
    wrapper_root = tmp_path / "wrap"
    scripts = wrapper_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(CRONJOB, scripts / "cronjob.sh")
    (wrapper_root / ".env").write_text(
        "HEALTHCHECKS_PING_KEY=KEY\nHEALTHCHECKS_BASE=https://hc.example.internal/ping\n"
    )
    script = _sh(tmp_path, 0)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    env.pop("HEALTHCHECKS_PING_KEY", None)
    env.pop("HEALTHCHECKS_BASE", None)
    result = subprocess.run(
        ["bash", str(scripts / "cronjob.sh"), job, "bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    lines = log.read_text().splitlines()
    assert len(lines) == 2, lines
    slug = "massive-" + job.replace("_", "-")
    assert f"https://hc.example.internal/ping/KEY/{slug}/start?create=1" in lines[0]
    assert f"https://hc.example.internal/ping/KEY/{slug}?create=1" in lines[1]


def test_bash_c_does_not_ping(tmp_path: Path) -> None:
    """`bash -c` is how tests take the lock, not a scheduled shell job."""
    job = _job_name()
    bin_dir, log = _fake_curl(tmp_path)
    result = _run(
        job,
        "bash",
        "-c",
        "exit 0",
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HEALTHCHECKS_PING_KEY": "KEY",
        },
    )
    assert result.returncode == 0
    assert not log.exists()


def test_python_job_does_not_get_wrapper_ping(tmp_path: Path) -> None:
    job = _job_name()
    bin_dir, log = _fake_curl(tmp_path)
    result = _run(
        job,
        "true",
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HEALTHCHECKS_PING_KEY": "KEY",
        },
    )
    assert result.returncode == 0
    assert not log.exists()


def test_skip_does_not_ping(tmp_path: Path) -> None:
    job = _job_name()
    lock = f"/tmp/massive-{job}.lock"
    bin_dir, log = _fake_curl(tmp_path)
    script = _sh(tmp_path, 0)
    holder = subprocess.Popen(
        ["bash", "-c", f"exec 9>'{lock}'; flock 9; printf 'HELD\\n'; sleep 60"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "HELD"
        result = _run(
            job,
            "bash",
            str(script),
            env={
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "HEALTHCHECKS_PING_KEY": "KEY",
            },
        )
        assert result.returncode == 0
        assert '"event":"job_skipped"' in result.stdout
        assert not log.exists()
    finally:
        holder.kill()
        holder.wait(timeout=5)


def test_wrapper_slug_matches_python_healthcheck_slug() -> None:
    from ingest.common.cli import healthcheck_slug

    assert healthcheck_slug("prune") == "massive-prune"
    assert healthcheck_slug("snapshot_sweep") == "massive-snapshot-sweep"
