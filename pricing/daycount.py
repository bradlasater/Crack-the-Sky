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
from datetime import date, timedelta
from typing import Protocol, runtime_checkable

from ingest.common.market_gate import is_weekday
from pricing.calendar import CalendarRangeError, SessionCalendar, load_session_calendar
from pricing.conventions import CALENDAR_DAYS_PER_YEAR, TRADING_DAYS_PER_YEAR


@runtime_checkable
class DayCount(Protocol):
    """A named vol-time convention.

    ``name`` identifies the convention; ``name_for`` identifies what actually
    produced one row, which is not the same thing once a convention can fall
    back (see :class:`HybridSessions`). Rows stamp ``name_for``.
    """

    name: str

    def year_fraction(self, start: date, end: date) -> float:
        """Vol time from ``start`` to ``end``, in years."""

    def name_for(self, start: date, end: date) -> str:
        """The convention that produced ``year_fraction(start, end)``."""


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

    def name_for(self, start: date, end: date) -> str:
        return self.name


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

    def name_for(self, start: date, end: date) -> str:
        return self.name


ACT_365 = CalendarDays()


@dataclass(frozen=True, slots=True)
class HybridSessions:
    """Sessions where the calendar can vouch for the span, ACT/365 beyond it.

    A strict session count refuses every expiry past the vendor's upcoming
    holiday window, which archive-wide is 3.7% of ``atm_term_structure`` rows
    and 4.4% of ``vol_surface`` rows -- and **none** of the 5-45 DTE book, whose
    expiries are always inside the horizon. Refusing to price the LEAPS tail
    rather than pricing it on the convention it already had is the worse trade,
    so the tail keeps ACT/365.

    The cost is that this convention is not a property of the pair alone: the
    horizon advances every time ``holidays_sync`` runs, so a span that falls
    back today may resolve to sessions on a later rebuild. That is precisely
    why rows stamp :meth:`name_for` rather than the convention's own ``name`` --
    an unstamped row would make the two indistinguishable, which is the silent
    archive mixing this whole change is trying to avoid.

    The fallback covers the horizon only. :class:`CalendarRangeError` also
    means a weekday stranded *between* attested history and the forward
    window, which is not a shape of the book but a sync job that has stopped
    running -- and it strikes the short end, where a one-session error in T is
    largest. Swallowing that would downgrade the 5-45 DTE book to ACT/365 and
    call it a normal day, so an interior gap is re-raised: the one refusal
    ``SessionCalendar`` makes that nothing here should be able to paper over.
    """

    sessions: TradingSessions
    fallback: CalendarDays = ACT_365
    name: str = "hybrid"

    def _spans_stale_gap(self, start: date, end: date) -> bool:
        """True when ``(start, end]`` contains a weekday no source reaches.

        The hole runs from the day after attested history to the day before
        the forward window opens, and only a *weekday* in it counts: the
        steady-state hole is exactly the Saturday between the Saturday and
        Sunday jobs, which needs no coverage and must not strand the LEAPS
        tail that happens to span it. A weekday in there is the stale-job
        signal, and it is the reason this is a scan and not a bounds test.
        """
        calendar = self.sessions.calendar
        if not calendar.sessions:
            return False
        # sessions_between consults the half-open (start, end].
        day = max(start + timedelta(days=1), max(calendar.sessions) + timedelta(days=1))
        through = min(end, calendar.forward_from - timedelta(days=1))
        while day <= through:
            if is_weekday(day):
                return True
            day += timedelta(days=1)
        return False

    def _resolve(self, start: date, end: date) -> tuple[float, str]:
        try:
            return self.sessions.year_fraction(start, end), self.sessions.name
        except CalendarRangeError:
            if self._spans_stale_gap(start, end):
                raise
            return self.fallback.year_fraction(start, end), self.fallback.name

    def year_fraction(self, start: date, end: date) -> float:
        return self._resolve(start, end)[0]

    def name_for(self, start: date, end: date) -> str:
        return self._resolve(start, end)[1]


# What every caller gets when it does not say. Changing this line changes every
# T in the day-bar path at once, which is why it is a line and not a literal.
DEFAULT_DAYCOUNT: DayCount = ACT_365


def hybrid_for(data_root: str | None = None) -> HybridSessions:
    """The session-count convention over the box's own calendar files."""
    return HybridSessions(TradingSessions(load_session_calendar(data_root)))


def discount_year_fraction(start: date, end: date) -> float:
    """Money time, ACT/365: rate-curve tenors and discount factors.

    Never a session count, and clamped at zero so a same-day or past expiry
    resolves the front of the curve rather than reflecting off it.
    """
    return max((end - start).days, 0) / float(CALENDAR_DAYS_PER_YEAR)
