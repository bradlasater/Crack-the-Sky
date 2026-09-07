"""Day-count conventions: how a date pair becomes the T in a pricing formula.

Two different quantities wear the same name in this repo, and conflating them
is the mistake this module exists to prevent.

**Vol time** is the T in σ√T. It measures how much *trading* falls between two
dates, because that is when prices move: a Saturday contributes no variance,
and neither does Labor Day. Whether it should therefore be counted in sessions
rather than calendar days is exactly the open question -- see
``docs/plans/trading-day-calendar.md``. This module makes the choice a passed
object instead of a module constant, so it can be made deliberately, measured
against the alternative, and recorded per landed row.

**Money time** is the T in e^(−rT) and the tenor used to look up the Treasury
curve. Interest accrues on weekends and holidays like every other day, so this
is always ACT/365 and never a session count. :func:`discount_year_fraction` is
the only spelling of it; converting one of those sites would both shorten the
discount factor and read the wrong point off the curve, and neither error is
visible in the output.

The two are already separate on the day-bar path -- ``term_structure`` and
``surface`` compute their rate tenor independently of the fit T -- and are
still fused on the live path, where ``from_market.year_fraction`` feeds one
value to both. Splitting that is its own change.

Not to be confused with ``GreeksConventions.trading_days`` in
``pricing.conventions``: that 252 only rescales theta *output* units in
``apply_conventions``. It has never touched a T that goes into a formula, and
adopting a session count here would not change it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from pricing.calendar import SessionCalendar
from pricing.conventions import CALENDAR_DAYS_PER_YEAR, TRADING_DAYS_PER_YEAR


@runtime_checkable
class DayCount(Protocol):
    """A named vol-time convention. ``name`` is what gets stamped on a row."""

    name: str

    def year_fraction(self, start: date, end: date) -> float:
        """Vol time from ``start`` to ``end``, in years."""


@dataclass(frozen=True, slots=True)
class CalendarDays:
    """ACT/365 -- calendar days to expiry over 365.

    The convention behind every row landed to date, and the default until a
    deliberate change says otherwise.
    """

    name: str = "act/365"
    days_per_year: float = float(CALENDAR_DAYS_PER_YEAR)

    def year_fraction(self, start: date, end: date) -> float:
        return (end - start).days / self.days_per_year


@dataclass(frozen=True, slots=True)
class TradingSessions:
    """Bus/252 -- sessions in ``(start, end]`` over 252, on a real calendar.

    Defined but not yet the default anywhere. It raises rather than guessing
    for a date its calendar cannot vouch for, which includes every expiry past
    the vendor's upcoming-holiday horizon -- so switching a caller to it is a
    decision about the long end of the book, not only about the convention.
    """

    calendar: SessionCalendar
    name: str = "bus/252"
    sessions_per_year: float = float(TRADING_DAYS_PER_YEAR)

    def year_fraction(self, start: date, end: date) -> float:
        return self.calendar.sessions_between(start, end) / self.sessions_per_year


ACT_365 = CalendarDays()

# What every caller gets when it does not say. Changing this line changes every
# T in the day-bar path at once, which is why it is a line and not a literal.
DEFAULT_DAYCOUNT: DayCount = ACT_365


def discount_year_fraction(start: date, end: date) -> float:
    """Money time, ACT/365: rate-curve tenors and discount factors.

    Never a session count, and clamped at zero so a same-day or past expiry
    resolves the front of the curve rather than reflecting off it.
    """
    return max((end - start).days, 0) / float(CALENDAR_DAYS_PER_YEAR)
