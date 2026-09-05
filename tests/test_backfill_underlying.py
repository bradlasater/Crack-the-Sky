"""Tests for the underlying backfill orchestrator.

It controls oldest-first ordering, resumability, retry and failure
classification, dataset selection and the nightly budget -- for the only
dataset besides option_snapshots that expires if it is not fetched in time.
A silent no-op here looks exactly like success.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

from ingest.common import landing
from ingest.common.http_client import NotEntitledError
from ingest.jobs.flatfile_pull import SESSION_ORACLE

ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location(
        "backfill_underlying", ROOT / "scripts" / "backfill_underlying.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _module()


SESSIONS = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]


def _seed(root: Path, sessions=SESSIONS) -> None:
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    landing.meta_path("flatfile_manifest.json", root).write_text(
        json.dumps([{"dataset": SESSION_ORACLE, "date": d, "md5": "x"} for d in sessions]),
        encoding="utf-8",
    )


def _land(root: Path, dataset: str, day: str) -> None:
    part = root / "clean" / dataset / f"dt={day}"
    part.mkdir(parents=True, exist_ok=True)
    (part / "x-1.parquet").write_bytes(b"")


class _Stub:
    """Stands in for a job module: records calls, writes on demand."""

    def __init__(self, root: Path, dataset: str, behaviour=None) -> None:
        self.root, self.dataset = root, dataset
        self.calls: list[date] = []
        self.behaviour = behaviour or (lambda day, n: "write")

    def _main_fn(self, args, settings, logger, *extra):
        day = date.fromisoformat(args.date)
        self.calls.append(day)
        what = self.behaviour(day, self.calls.count(day))
        if isinstance(what, Exception):
            raise what
        if what == "write":
            _land(self.root, self.dataset, day.isoformat())


# Frozen: the entitlement boundary is measured from the clock, so a suite
# that reads the real one tests a different window every day.
TODAY = date(2026, 9, 4)


def _install(mod, monkeypatch, tmp_path, behaviour=None,
             dataset="underlying_minute_bars", today=TODAY):
    stub = _Stub(tmp_path, dataset, behaviour)
    monkeypatch.setattr(mod, "_today", lambda: today)
    monkeypatch.setattr(mod, "JOBS", ((dataset, stub, False),))
    monkeypatch.setattr(mod, "ping", lambda *a, **k: None)
    monkeypatch.setattr(mod, "healthcheck_url", lambda *a, **k: (None, False))
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    return stub


def _run(mod, extra=None):
    return mod.main([SESSIONS[0], SESSIONS[-1], *(extra or [])])


def test_walks_oldest_first(mod, monkeypatch, tmp_path) -> None:
    """The old edge is the part that expires, so it is secured first."""
    _seed(tmp_path)
    stub = _install(mod, monkeypatch, tmp_path)
    assert _run(mod) == 0
    assert [d.isoformat() for d in stub.calls] == SESSIONS


def test_existing_partitions_are_skipped_without_a_request(mod, monkeypatch, tmp_path) -> None:
    _seed(tmp_path)
    stub = _install(mod, monkeypatch, tmp_path)
    for day in SESSIONS[:3]:
        _land(tmp_path, "underlying_minute_bars", day)
    assert _run(mod) == 0
    assert [d.isoformat() for d in stub.calls] == SESSIONS[3:]


def test_403_is_recorded_as_unfetchable_not_a_failure(mod, monkeypatch, tmp_path) -> None:
    """Expected at the old edge; it must not fail the nightly run."""
    _seed(tmp_path)
    stub = _install(
        mod, monkeypatch, tmp_path,
        behaviour=lambda day, n: NotEntitledError("403") if day.isoformat() == SESSIONS[0] else "write",
    )
    assert _run(mod) == 0
    # One attempt only: a 403 is not retried.
    assert sum(1 for d in stub.calls if d.isoformat() == SESSIONS[0]) == 1


def test_a_filesystem_permission_error_is_fatal(mod, monkeypatch, tmp_path) -> None:
    """The distinction Copilot caught: EACCES on a write is not an entitlement miss."""
    _seed(tmp_path)
    _install(mod, monkeypatch, tmp_path,
             behaviour=lambda day, n: PermissionError(13, "Permission denied"))
    assert _run(mod) == 1, "a disk permission failure was filed as 'unfetchable'"


def test_transient_errors_are_retried_then_recorded(mod, monkeypatch, tmp_path) -> None:
    _seed(tmp_path)
    stub = _install(mod, monkeypatch, tmp_path,
                    behaviour=lambda day, n: RuntimeError("boom"))
    assert _run(mod) == 1
    assert sum(1 for d in stub.calls if d.isoformat() == SESSIONS[0]) == mod.ATTEMPTS


def test_a_retried_error_that_then_succeeds_is_not_a_failure(mod, monkeypatch, tmp_path) -> None:
    _seed(tmp_path)
    _install(mod, monkeypatch, tmp_path,
             behaviour=lambda day, n: RuntimeError("boom") if n == 1 else "write")
    assert _run(mod) == 0


def test_success_without_a_partition_is_retried_and_reported(mod, monkeypatch, tmp_path) -> None:
    """The silent hole: returning 0 rows wrote nothing and exited 0 anyway."""
    _seed(tmp_path)
    stub = _install(mod, monkeypatch, tmp_path, behaviour=lambda day, n: "nothing")
    assert _run(mod) == 1, "an expiring session went missing while the run exited 0"
    assert sum(1 for d in stub.calls if d.isoformat() == SESSIONS[0]) == mod.ATTEMPTS


def test_max_sessions_bounds_the_nightly_bite(mod, monkeypatch, tmp_path) -> None:
    _seed(tmp_path)
    stub = _install(mod, monkeypatch, tmp_path)
    assert _run(mod, ["--max-sessions", "2"]) == 0
    assert [d.isoformat() for d in stub.calls] == SESSIONS[:2]


def test_max_sessions_counts_work_not_skips(mod, monkeypatch, tmp_path) -> None:
    """Otherwise a resumed run burns its whole budget on already-done days."""
    _seed(tmp_path)
    stub = _install(mod, monkeypatch, tmp_path)
    for day in SESSIONS[:3]:
        _land(tmp_path, "underlying_minute_bars", day)
    assert _run(mod, ["--max-sessions", "2"]) == 0
    assert [d.isoformat() for d in stub.calls] == SESSIONS[3:5]


def test_unknown_dataset_is_rejected(mod, monkeypatch, tmp_path) -> None:
    _seed(tmp_path)
    _install(mod, monkeypatch, tmp_path)
    assert _run(mod, ["--datasets", "nope"]) == 2


def test_no_manifest_refuses_to_guess_sessions(mod, monkeypatch, tmp_path) -> None:
    """Without the oracle, past holidays would be requested as sessions."""
    _install(mod, monkeypatch, tmp_path)
    assert _run(mod) == 2


def test_sessions_come_from_the_manifest_not_the_calendar(mod, monkeypatch, tmp_path) -> None:
    """A weekday the vendor never published must not be requested."""
    published = [d for d in SESSIONS if d != "2026-08-26"]
    _seed(tmp_path, published)
    stub = _install(mod, monkeypatch, tmp_path)
    assert _run(mod) == 0
    assert "2026-08-26" not in [d.isoformat() for d in stub.calls]


# ---------------------------------------------------------------------------
# The window must move with the entitlement boundary
#
# A pinned start date drifts past the boundary at one session a day, and the
# job then spends its nightly budget re-requesting sessions that have already
# expired -- forever, on data that no longer exists.
# ---------------------------------------------------------------------------

def _window_start(mod, today: date = TODAY) -> date:
    from datetime import timedelta

    return today - timedelta(days=mod.UNDERLYING_ENTITLEMENT_DAYS)


def test_no_arguments_derives_the_whole_current_window(mod, monkeypatch, tmp_path) -> None:
    """How cron invokes it: no dates at all."""
    from datetime import timedelta

    yesterday = TODAY - timedelta(days=1)
    boundary = _window_start(mod)
    just_inside = boundary + timedelta(days=1)
    just_outside = boundary - timedelta(days=1)
    _seed(tmp_path, [d.isoformat() for d in (just_outside, boundary, just_inside, yesterday)])
    stub = _install(mod, monkeypatch, tmp_path)

    assert mod.main([]) == 0
    asked = [d.isoformat() for d in stub.calls]
    assert just_outside.isoformat() not in asked, "asked for an expired session"
    assert boundary.isoformat() in asked, "did not reach back to the boundary"
    assert yesterday.isoformat() in asked, "did not reach forward to yesterday"


def test_a_start_older_than_the_boundary_is_clamped(mod, monkeypatch, tmp_path, capsys) -> None:
    end = TODAY - __import__("datetime").timedelta(days=1)
    boundary = _window_start(mod)
    ancient = date(2020, 1, 2)
    _seed(tmp_path, [ancient.isoformat(), boundary.isoformat(), end.isoformat()])
    stub = _install(mod, monkeypatch, tmp_path)
    assert mod.main([ancient.isoformat(), end.isoformat()]) == 0
    asked = [d.isoformat() for d in stub.calls]
    assert ancient.isoformat() not in asked, (
        "requested a session that is past the entitlement boundary"
    )
    assert boundary.isoformat() in asked
    assert "clamping" in capsys.readouterr().out


def test_the_derived_window_matches_what_coverage_audit_audits(mod) -> None:
    """One constant, so the backfill and the audit cannot disagree."""
    from ingest.jobs.coverage_audit import UNDERLYING_ENTITLEMENT_DAYS

    assert mod.UNDERLYING_ENTITLEMENT_DAYS == UNDERLYING_ENTITLEMENT_DAYS


def test_the_scheduled_command_pins_no_dates(mod) -> None:
    """The regression this guards: a literal start date in the schedule."""
    import re

    schedule = json.loads((ROOT / "deploy" / "schedule.json").read_text())
    unit = next(u for u in schedule["units"] if u["job"] == "backfill_underlying")
    assert not any(re.search(r"\d{4}-\d{2}-\d{2}", arg) for arg in unit["command"]), (
        f"the schedule pins a date, which will drift past the boundary: {unit['command']}"
    )


def test_a_historical_end_does_not_slide_the_boundary(mod, monkeypatch, tmp_path) -> None:
    """The bug Copilot caught: `end` caps the range, it does not move the wall.

    Deriving the boundary from `end` meant asking for an older end quietly
    re-admitted sessions that had already expired -- exactly the wasted
    requests this script exists to avoid.
    """
    from datetime import timedelta

    boundary = _window_start(mod)
    expired = boundary - timedelta(days=5)
    end = TODAY - timedelta(days=200)
    _seed(tmp_path, [expired.isoformat(), boundary.isoformat(), end.isoformat()])
    stub = _install(mod, monkeypatch, tmp_path)

    assert mod.main([expired.isoformat(), end.isoformat()]) == 0
    asked = [d.isoformat() for d in stub.calls]
    assert expired.isoformat() not in asked, (
        "a historical --end slid the boundary back and admitted an expired session"
    )
    assert boundary.isoformat() in asked


def test_a_future_end_does_not_skip_fetchable_sessions(mod, monkeypatch, tmp_path) -> None:
    from datetime import timedelta

    boundary = _window_start(mod)
    end = TODAY + timedelta(days=90)
    _seed(tmp_path, [boundary.isoformat(), (TODAY - timedelta(days=1)).isoformat()])
    stub = _install(mod, monkeypatch, tmp_path)
    assert mod.main([boundary.isoformat(), end.isoformat()]) == 0
    assert boundary.isoformat() in [d.isoformat() for d in stub.calls]
