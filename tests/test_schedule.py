"""deploy/schedule.json carries every schedule twice: the cron form (the
installed crontab stays in place through the cutover overlap) and the systemd
OnCalendar form (the generated timers). These tests make that duplication safe:

* the cron forms and commands must reproduce deploy/crontab exactly, so the
  crontab and the schedule file cannot drift apart while both are installed;
* the OnCalendar forms must fire at the same instants as the cron forms,
  checked against ``systemd-analyze calendar --iterations=N`` where that binary
  exists (ubuntu-latest CI has it; a macOS dev box skips);
* structural rules from the design: one healthchecks block per job, and
  Restart= only where the next tick is far away.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from ingest.common.market_gate import ET
from tests.test_healthchecks import _expand

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEDULE = json.loads((REPO_ROOT / "deploy" / "schedule.json").read_text())
UNITS: list[dict] = SCHEDULE["units"]

# Above this many fires per firing day, Restart=on-failure is pointless: the
# next scheduled tick is closer than any sane RestartSec. snapshot_sweep (1/min)
# and trades_watchlist (5/min-ish) are the jobs this keeps restart-free.
MAX_FIRES_PER_DAY_FOR_RESTART = 4


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_unit_schema_and_uniqueness() -> None:
    seen: set[str] = set()
    for u in UNITS:
        assert set(u) >= {"job", "unit", "command", "cron", "on_calendar", "healthchecks", "restart"}
        assert u["unit"] not in seen, f"duplicate unit {u['unit']}"
        seen.add(u["unit"])
        assert u["unit"].startswith("massive-" + u["job"].replace("_", "-"))
        assert u["command"] and all(isinstance(arg, str) for arg in u["command"])
        assert len(u["cron"]) == len(u["on_calendar"]) >= 1, (
            f"{u['unit']}: cron and on_calendar pair up one-to-one"
        )
        if u["restart"] is not None:
            assert set(u["restart"]) == {"sec"} and u["restart"]["sec"] > 0


def test_every_job_has_exactly_one_healthcheck_block() -> None:
    """Shared-slug variants (--expired, --eod) ping the parent job's check, so
    the block lives on exactly one unit per job; bash jobs (prune) are
    deliberately unmonitored and have none."""
    for u in UNITS:
        if u["command"][0] == "bash":
            assert u["healthchecks"] is None, f"{u['unit']}: shell jobs are unmonitored"
    jobs_with_block = [u["job"] for u in UNITS if u["healthchecks"] is not None]
    assert len(jobs_with_block) == len(set(jobs_with_block)), "one healthchecks block per job"


def test_extra_checks_are_owned_by_scheduled_jobs() -> None:
    jobs = {u["job"] for u in UNITS}
    for name, check in SCHEDULE["extra_checks"].items():
        assert name not in jobs, f"{name} is a scheduled job, not a job-pinged check"
        assert check["owner"] in jobs, f"{name} has no scheduled owner"
        assert set(check) >= {"schedule", "grace_min", "desc"}


def _fires_per_firing_day(unit: dict) -> int:
    """Distinct (hour, minute) instants the unit's cron forms fire on a matched day."""
    instants: set[tuple[int, int]] = set()
    for expr in unit["cron"]:
        minute, hour = expr.split()[:2]
        for hh in _expand(hour, 0, 23):
            for mm in _expand(minute, 0, 59):
                instants.add((hh, mm))
    return len(instants)


def test_restart_only_where_the_next_tick_is_far_away() -> None:
    """A retry on a 1/min job is pointless -- the next tick is seconds away --
    and a daily job with no Restart= waits a full day on a transient failure."""
    for unit in UNITS:
        fires = _fires_per_firing_day(unit)
        if fires > MAX_FIRES_PER_DAY_FOR_RESTART:
            assert unit["restart"] is None, (
                f"{unit['unit']}: {fires} fires/day, a retry would collide with the next tick"
            )
        else:
            assert unit["restart"] is not None, (
                f"{unit['unit']}: {fires} fires/day, a failure otherwise waits for the next tick"
            )


# ---------------------------------------------------------------------------
# While both are installed: schedule.json must reproduce deploy/crontab
# ---------------------------------------------------------------------------


def _crontab_schedule() -> dict[tuple[str, tuple[str, ...]], list[str]]:
    """(job, command tokens) -> cron expressions, from deploy/crontab.

    Normalized to schedule.json's shape: ``$PY`` (the venv python) is dropped
    from the command and the ``>> $LOG 2>&1`` redirect goes with the shell
    wrapping, leaving the tokens cronjob.sh actually invokes.
    """
    out: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for line in (REPO_ROOT / "deploy" / "crontab").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if not re.match(r"[\d*]", fields[0]):
            continue  # environment assignment, not a schedule
        m = re.match(
            r"cd \$REPO && bash scripts/cronjob\.sh (\w+) (.+) >> \$LOG 2>&1$",
            " ".join(fields[5:]),
        )
        assert m, f"crontab line does not match the cronjob.sh wrapper: {line}"
        job, command = m.group(1), m.group(2).split()
        if command[0] == "$PY":
            command = command[1:]
        out.setdefault((job, tuple(command)), []).append(" ".join(fields[:5]))
    return out


def test_schedule_json_reproduces_the_installed_crontab() -> None:
    """The follow-up PR deletes deploy/crontab; until then they cannot drift."""
    from_json: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for unit in UNITS:
        key = (unit["job"], tuple(unit["command"]))
        from_json.setdefault(key, []).extend(unit["cron"])
    from_json = {k: sorted(v) for k, v in from_json.items()}
    from_crontab = {k: sorted(v) for k, v in _crontab_schedule().items()}
    assert from_json == from_crontab


# ---------------------------------------------------------------------------
# cron and on_calendar must fire at the same instants
# ---------------------------------------------------------------------------

SYSTEMD_ANALYZE = shutil.which("systemd-analyze")

_ELAPSE_RE = re.compile(
    r"(?:Next elapse|Iter(?:ation)?\.?\s+#\d+):\s+\w{3}\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})"
)


def _cron_fire_times(expr: str, start: datetime, horizon: timedelta) -> list[datetime]:
    """Instants (naive ET wall clock) a cron expression fires on in [start, start+horizon]."""
    minute, hour, dom, month, dow = expr.split()
    minutes = set(_expand(minute, 0, 59))
    hours = set(_expand(hour, 0, 23))
    doms = set(_expand(dom, 1, 31))
    months = set(_expand(month, 1, 12))
    dows = {d % 7 for d in _expand(dow, 0, 7)}  # cron accepts both 0 and 7 for Sunday
    dom_restricted, dow_restricted = dom != "*", dow != "*"

    fires: list[datetime] = []
    end = start + horizon
    day = start.date()
    while datetime.combine(day, time.min) <= end:
        cron_dow = (day.weekday() + 1) % 7  # datetime: Mon=0; cron: Sun=0
        if dom_restricted and dow_restricted:
            # cron's OR rule when both day fields are restricted.
            day_matches = day.day in doms or cron_dow in dows
        elif dom_restricted:
            day_matches = day.day in doms
        elif dow_restricted:
            day_matches = cron_dow in dows
        else:
            day_matches = True
        if day_matches and day.month in months:
            for hh in sorted(hours):
                for mm in sorted(minutes):
                    dt = datetime.combine(day, time(hh, mm))
                    if start <= dt <= end:
                        fires.append(dt)
        day += timedelta(days=1)
    return fires


def _systemd_fire_times(expr: str, count: int) -> list[datetime]:
    """The next `count` elapses of an OnCalendar expression, as naive ET wall clock."""
    out = subprocess.run(
        [SYSTEMD_ANALYZE, "calendar", f"--iterations={count}", expr],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "TZ": "America/New_York"},
    ).stdout
    fires = [
        datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S")
        for d, t in _ELAPSE_RE.findall(out)
    ]
    assert len(fires) == count, f"parsed {len(fires)} elapses, expected {count}:\n{out}"
    return fires


def _dst_shift_day(d: date) -> bool:
    """True if the ET offset changes during calendar day d (spring/fall transition)."""
    midnight = datetime.combine(d, time.min, tzinfo=ET)
    return midnight.utcoffset() != (midnight + timedelta(days=1)).utcoffset()


@pytest.mark.skipif(SYSTEMD_ANALYZE is None, reason="systemd-analyze not installed")
@pytest.mark.parametrize("unit", UNITS, ids=[u["unit"] for u in UNITS])
def test_on_calendar_fires_at_the_same_instants_as_cron(unit: dict) -> None:
    # Two minutes of margin: systemd-analyze computes from its own now, so
    # instants right at the boundary could fall on either side of it.
    threshold = (datetime.now() + timedelta(minutes=2)).replace(microsecond=0)
    for cron_expr, cal_expr in zip(unit["cron"], unit["on_calendar"], strict=True):
        cron_fires = _cron_fire_times(cron_expr, threshold, timedelta(days=9))
        if len(cron_fires) < 3:
            # Weekly/monthly entries need a longer window to fire enough times
            # to prove anything.
            cron_fires = _cron_fire_times(cron_expr, threshold, timedelta(days=95))
        systemd_fires = _systemd_fire_times(cal_expr, len(cron_fires))
        # On the two DST-shift days a year, cron and systemd legitimately
        # disagree on the skipped/repeated hour; expression equivalence is not
        # what differs there, so those days are excluded from the comparison.
        expected = [f for f in cron_fires if not _dst_shift_day(f.date())]
        actual = [f for f in systemd_fires if not _dst_shift_day(f.date())]
        assert actual == expected, (
            f"{unit['unit']}: cron {cron_expr!r} and on_calendar {cal_expr!r} diverge"
        )
