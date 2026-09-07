"""Session calendar: attested history, the forward window, and the raise between.

Fixtures are verbatim snapshots of the box's own ``_meta`` files as of
2026-09-06 -- ``trading_days.json`` (1048 attested weekdays, 41 of them
closures) and the ``/v1/marketstatus/upcoming`` window that opens on Labor Day
2026 and ends 2027-07-05. Real data, so the closure cases below are the ones
the vendor actually publishes rather than ones invented to pass.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from pricing.calendar import (
    CalendarRangeError,
    CalendarSourceError,
    SessionCalendar,
    load_session_calendar,
)
from tests.conftest import load_fixture

# The two jobs' steady-state cadence, which is what makes the gap a weekend:
# history_audit runs Sat 13:00 ET verifying through Friday, holidays_sync Sun
# 07:00 ET. The fixtures were taken on the Sunday, so these are its real values.
ATTESTED_THROUGH = date(2026, 9, 4)  # Friday
HOLIDAYS_FETCHED = date(2026, 9, 6)  # Sunday
HOLIDAYS_THROUGH = date(2027, 7, 5)

LABOR_DAY = date(2026, 9, 7)
THANKSGIVING = date(2026, 11, 26)
BLACK_FRIDAY = date(2026, 11, 27)  # early close, 13:00 ET -- still a session


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """A DATA_ROOT laid out the way the box lays it out."""
    meta = tmp_path / "_meta"
    meta.mkdir()
    for name, fixture in (
        ("trading_days.json", "trading_days.json"),
        ("holidays.json", "holidays_upcoming_2026.json"),
    ):
        (meta / name).write_text(json.dumps(load_fixture(fixture)), encoding="utf-8")
    # holidays_sync lands the raw response beside the cache; its newest
    # partition is how the calendar learns when the window was fetched.
    (tmp_path / "raw" / "holidays" / f"dt={HOLIDAYS_FETCHED}").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def calendar(data_root: Path) -> SessionCalendar:
    return load_session_calendar(data_root)


def test_window_bounds_come_from_the_sources(calendar: SessionCalendar) -> None:
    assert calendar.forward_from == HOLIDAYS_FETCHED
    assert calendar.forward_through == HOLIDAYS_THROUGH
    assert max(calendar.sessions) == ATTESTED_THROUGH


# ---------------------------------------------------------------------------
# The forward window
# ---------------------------------------------------------------------------


def test_labor_day_is_not_a_session(calendar: SessionCalendar) -> None:
    """The first record in the upcoming window, and a Monday -- so the weekday
    rule alone would call it a session."""
    assert LABOR_DAY.weekday() == 0
    assert calendar.is_session(LABOR_DAY) is False


def test_thanksgiving_closes_and_black_friday_is_a_short_session(
    calendar: SessionCalendar,
) -> None:
    assert calendar.is_session(THANKSGIVING) is False
    # An early close is a session: the market opens. Only its length differs,
    # and this module reports that separately rather than folding it into a
    # fractional session count.
    assert calendar.is_session(BLACK_FRIDAY) is True
    assert calendar.is_early_close(BLACK_FRIDAY) is True
    assert calendar.is_early_close(date(2026, 12, 23)) is False


def test_ordinary_forward_weekday_is_a_session(calendar: SessionCalendar) -> None:
    assert calendar.is_session(date(2026, 9, 8)) is True


# ---------------------------------------------------------------------------
# Attested history
# ---------------------------------------------------------------------------


def test_agrees_with_every_attested_day(calendar: SessionCalendar) -> None:
    """The regression pin: 1048 real weekdays, none of them re-derived.

    The union must never let the weekday rule or the forward window override
    what the vendor attested -- including the 41 closures, which are the only
    record the repo has of ad-hoc shutdowns (a national day of mourning is on
    nobody's rrule).
    """
    attested = {date.fromisoformat(k): v for k, v in load_fixture("trading_days.json").items()}
    assert len(attested) == 1048
    assert sum(1 for v in attested.values() if not v) == 41
    mismatched = [d for d, was in attested.items() if calendar.is_session(d) is not was]
    assert mismatched == []


def test_attested_closure_beats_the_weekday_rule(calendar: SessionCalendar) -> None:
    """A past Monday holiday resolves from history, not from holidays.json --
    which has long since dropped it from the upcoming window."""
    past_labor_day = date(2025, 9, 1)
    assert past_labor_day.weekday() == 0
    assert past_labor_day not in calendar.holidays
    assert calendar.is_session(past_labor_day) is False


# ---------------------------------------------------------------------------
# Weekends, and the gap between the two sources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("day", [date(2026, 9, 5), date(2027, 12, 25), date(2019, 1, 5)])
def test_weekends_never_need_coverage(calendar: SessionCalendar, day: date) -> None:
    """Answered before either source is consulted, which is what keeps the
    routine Sat/Sun handoff between the two jobs quiet -- 2026-09-05 is the
    Saturday that falls in the gap, and the other two are far outside every
    window."""
    assert day.weekday() >= 5
    assert calendar.is_session(day) is False


def test_weekday_in_the_gap_raises(calendar: SessionCalendar) -> None:
    """A weekday between attested history and the fetch means a job is behind.

    Guessing here is the dangerous case: a closure that has already passed is
    gone from the upcoming window, so weekday-minus-holidays would call it a
    session and every T built on it would be one session long.
    """
    stale = SessionCalendar(
        sessions={d: v for d, v in calendar.sessions.items() if d < date(2026, 8, 28)},
        holidays=calendar.holidays,
        early_closes=calendar.early_closes,
        forward_from=calendar.forward_from,
        forward_through=calendar.forward_through,
    )
    with pytest.raises(CalendarRangeError, match="not covered"):
        stale.is_session(date(2026, 8, 31))


def test_beyond_the_forward_horizon_raises(calendar: SessionCalendar) -> None:
    """Past the last record the window's end and 'no more holidays' are
    indistinguishable, so the answer is refused rather than assumed."""
    # The last record is itself a closure -- observed Independence Day, a
    # Monday -- so the boundary answers, and only the day after it refuses.
    assert calendar.is_session(HOLIDAYS_THROUGH) is False
    with pytest.raises(CalendarRangeError, match="not covered"):
        calendar.is_session(date(2027, 7, 6))


def test_before_attested_history_raises(calendar: SessionCalendar) -> None:
    with pytest.raises(CalendarRangeError, match="not covered"):
        calendar.is_session(date(2022, 8, 30))


def test_early_close_outside_the_window_raises(calendar: SessionCalendar) -> None:
    """trading_days.json records whether a date was a session, never how long
    it ran, so a past half day would otherwise silently read as a full one."""
    with pytest.raises(CalendarRangeError, match="not recorded"):
        calendar.is_early_close(date(2025, 11, 28))


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


def test_sessions_between_is_half_open(calendar: SessionCalendar) -> None:
    start = date(2026, 9, 8)
    assert calendar.sessions_between(start, start) == 0
    assert calendar.sessions_between(start, date(2026, 9, 9)) == 1


def test_sessions_between_skips_the_weekend_and_the_holiday(
    calendar: SessionCalendar,
) -> None:
    """Sun 09-06 to Fri 09-11: Labor Day out, four sessions left -- against
    five calendar days, which is the whole reason for this module."""
    assert calendar.sessions_between(date(2026, 9, 6), date(2026, 9, 11)) == 4
    assert (date(2026, 9, 11) - date(2026, 9, 6)).days == 5


def test_sessions_between_chains(calendar: SessionCalendar) -> None:
    """Half-open intervals must add up, or a term structure built expiry by
    expiry would double-count every boundary."""
    a, b, c = date(2026, 9, 8), date(2026, 9, 30), date(2026, 10, 16)
    assert calendar.sessions_between(a, b) + calendar.sessions_between(b, c) == (
        calendar.sessions_between(a, c)
    )


def test_sessions_between_rejects_a_reversed_interval(calendar: SessionCalendar) -> None:
    with pytest.raises(ValueError, match="precedes"):
        calendar.sessions_between(date(2026, 9, 30), date(2026, 9, 8))


def test_sessions_between_raises_on_an_uncovered_interval(
    calendar: SessionCalendar,
) -> None:
    """A partial count is worse than none: it is indistinguishable from a
    genuinely short stretch."""
    with pytest.raises(CalendarRangeError):
        calendar.sessions_between(date(2027, 7, 1), date(2027, 7, 20))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_missing_raw_partition_refuses_to_load(data_root: Path) -> None:
    """Without the fetch date the forward window has no start, and a dropped
    closure would read as a session -- so the calendar declines to exist."""
    (data_root / "raw" / "holidays" / f"dt={HOLIDAYS_FETCHED}").rmdir()
    with pytest.raises(CalendarSourceError, match="was fetched is unknown"):
        load_session_calendar(data_root)


def test_missing_holidays_refuses_to_load(data_root: Path) -> None:
    (data_root / "_meta" / "holidays.json").unlink()
    with pytest.raises(CalendarSourceError, match="forward window"):
        load_session_calendar(data_root)


def test_newest_raw_partition_wins(data_root: Path) -> None:
    """holidays_sync lands one partition per run and prune_raw keeps them all."""
    (data_root / "raw" / "holidays" / "dt=2026-08-31").mkdir()
    (data_root / "raw" / "holidays" / "dt=not-a-date").mkdir()
    assert load_session_calendar(data_root).forward_from == HOLIDAYS_FETCHED


def test_unparseable_attested_key_is_dropped(data_root: Path) -> None:
    """A bad key attests to nothing, so it is dropped rather than failing the
    whole load. The date it meant is then uncovered, which raises when asked
    about -- the loud outcome, not the weekday rule's guess."""
    meta = data_root / "_meta" / "trading_days.json"
    days = json.loads(meta.read_text(encoding="utf-8"))
    days["2024-13-45"] = True
    meta.write_text(json.dumps(days), encoding="utf-8")
    calendar = load_session_calendar(data_root)
    assert len(calendar.sessions) == 1048
