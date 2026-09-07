"""Fixtures shared by the pricing tests that need a real session calendar.

The calendar is built from verbatim snapshots of the box's own ``_meta`` files
as of 2026-09-06, so the closures and half days below are the ones the vendor
actually publishes rather than ones invented to pass.
"""

from __future__ import annotations

from datetime import date

import pytest

from pricing.calendar import SessionCalendar
from tests.conftest import load_fixture


@pytest.fixture
def session_calendar() -> SessionCalendar:
    """The box's own calendar files, as of 2026-09-06."""
    holidays = load_fixture("holidays_upcoming_2026.json")
    closed = {date.fromisoformat(r["date"]) for r in holidays if r["status"] == "closed"}
    early = {date.fromisoformat(r["date"]) for r in holidays if r["status"] != "closed"}
    return SessionCalendar(
        sessions={date.fromisoformat(k): v
                  for k, v in load_fixture("trading_days.json").items()},
        holidays=frozenset(closed),
        early_closes=frozenset(early),
        forward_from=date(2026, 9, 6),
        forward_through=max(closed | early),
    )
