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
        assert set(u) >= {
            "job",
            "unit",
            "command",
            "cron",
            "on_calendar",
            "healthchecks",
            "restart",
        }
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
    the block lives on exactly one unit per job. Every scheduled job is
    monitored, including the bash prune job."""
    jobs_with_block = [u["job"] for u in UNITS if u["healthchecks"] is not None]
    assert len(jobs_with_block) == len(set(jobs_with_block)), "one healthchecks block per job"
    assert set(jobs_with_block) == {u["job"] for u in UNITS}, (
        "scheduled but unmonitored: "
        + str(sorted({u["job"] for u in UNITS} - set(jobs_with_block)))
    )


def test_extra_checks_are_owned_by_scheduled_jobs() -> None:
    jobs = {u["job"] for u in UNITS}
    for name, check in SCHEDULE["extra_checks"].items():
        assert name not in jobs, f"{name} is a scheduled job, not a job-pinged check"
        assert check["owner"] in jobs, f"{name} has no scheduled owner"
        assert set(check) >= {"schedule", "grace_min", "desc"}


def test_all_chains_check_uses_the_owner_session_schedule() -> None:
    """One Healthchecks cron cannot say 09:30-16:30, so extra checks use the
    same liquid-session expression as the owner. An open/close outage that
    recovers before 10:00 is the same gap the primary check already accepts.
    """
    owner = next(
        u["healthchecks"]
        for u in UNITS
        if u["job"] == "snapshot_sweep" and u["healthchecks"] is not None
    )
    extra = SCHEDULE["extra_checks"]["snapshot_sweep_all_chains"]
    assert extra["schedule"] == owner["schedule"]
    assert extra["grace_min"] == owner["grace_min"]
    assert "10:00-15:59" in extra["desc"]


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


def test_prune_is_monitored_on_its_monthly_schedule() -> None:
    prune = next(u for u in UNITS if u["job"] == "prune")
    assert prune["command"][0] == "bash"
    assert prune["healthchecks"] is not None
    assert prune["healthchecks"]["schedule"] == "15 3 1 * *"
    assert prune["cron"] == ["15 03 1 * *"]
    assert prune["healthchecks"]["grace_min"] == 180


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

# Both sides of the comparison start here: now plus a margin. systemd-analyze
# samples its own clock a moment after Python samples ours, so a fire instant
# falling between the two samples would land in one list and not the other.
# Starting both lists a little in the future puts that race entirely behind the
# start line.
_MARGIN = timedelta(minutes=2)

# The elapses the margin covers have to be discarded from the systemd side, so
# ask for that many extra. Every schedule in the file fires on a whole minute
# and at most once a minute, which bounds how many the margin can hide; the
# helper below asserts that bound rather than trusting it.
_MARGIN_ELAPSES = int(_MARGIN.total_seconds() // 60) + 1


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


def _systemd_fire_times(expr: str, count: int, since: datetime) -> list[datetime]:
    """The first `count` elapses of an OnCalendar expression at or after `since`.

    ``--iterations`` always counts from systemd's own now, which is behind
    `since` by the margin, so the leading elapses the margin covers are asked
    for and then dropped. Without that the two sides start at different
    instants, and any schedule that fires within the margin -- the by-the-minute
    sweeps -- diverges by construction.
    """
    requested = count + _MARGIN_ELAPSES
    out = subprocess.run(
        [SYSTEMD_ANALYZE, "calendar", f"--iterations={requested}", expr],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "TZ": "America/New_York"},
    ).stdout
    fires = [datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S") for d, t in _ELAPSE_RE.findall(out)]
    assert len(fires) == requested, f"parsed {len(fires)} elapses, expected {requested}:\n{out}"
    trimmed = [f for f in fires if f >= since]
    assert len(trimmed) >= count, (
        f"{expr!r}: the {_MARGIN} margin hid more than {_MARGIN_ELAPSES} elapses, "
        f"so the schedule fires more often than once a minute:\n{out}"
    )
    return trimmed[:count]


def _dst_shift_day(d: date) -> bool:
    """True if the ET offset changes during calendar day d (spring/fall transition)."""
    midnight = datetime.combine(d, time.min, tzinfo=ET)
    return midnight.utcoffset() != (midnight + timedelta(days=1)).utcoffset()


@pytest.mark.skipif(SYSTEMD_ANALYZE is None, reason="systemd-analyze not installed")
@pytest.mark.parametrize("unit", UNITS, ids=[u["unit"] for u in UNITS])
def test_on_calendar_fires_at_the_same_instants_as_cron(unit: dict) -> None:
    # Both lists start at this same instant -- see _MARGIN. Naive ET wall clock:
    # systemd-analyze runs with TZ=America/New_York, so seed the cron side from
    # ET too -- a UTC-local now (CI runners) can be a calendar day ahead and
    # shift the whole comparison.
    threshold = (datetime.now(ET) + _MARGIN).replace(microsecond=0, tzinfo=None)
    for cron_expr, cal_expr in zip(unit["cron"], unit["on_calendar"], strict=True):
        cron_fires = _cron_fire_times(cron_expr, threshold, timedelta(days=9))
        if len(cron_fires) < 3:
            # Weekly/monthly entries need a longer window to fire enough times
            # to prove anything.
            cron_fires = _cron_fire_times(cron_expr, threshold, timedelta(days=95))
        systemd_fires = _systemd_fire_times(cal_expr, len(cron_fires), threshold)
        # On the two DST-shift days a year, cron and systemd legitimately
        # disagree on the skipped/repeated hour; expression equivalence is not
        # what differs there, so those days are excluded from the comparison.
        expected = [f for f in cron_fires if not _dst_shift_day(f.date())]
        actual = [f for f in systemd_fires if not _dst_shift_day(f.date())]
        assert actual == expected, (
            f"{unit['unit']}: cron {cron_expr!r} and on_calendar {cal_expr!r} diverge"
        )
