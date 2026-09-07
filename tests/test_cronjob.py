"""scripts/cronjob.sh: lock skip vs the wrapped command's own exit status.

The wrapper used to run `flock -n -E 99`, so a command that exited 99 was
logged as job_skipped and swallowed to 0. The lock is now taken on fd 9
before the command runs, which makes those two outcomes distinct.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CRONJOB = REPO / "scripts" / "cronjob.sh"


def _job_name() -> str:
    return "cjtest_" + uuid.uuid4().hex[:12]


def _run(job: str, *command: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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
