"""Day-count conventions: how a date pair becomes the T in a pricing formula.

Two different quantities wear the same name in this repo, and conflating them
is the mistake this module exists to prevent.

**Vol time** is the T in σ√T. It measures how much *trading* falls between two
dates, because that is when prices move: a Saturday contributes no variance,
and neither does Labor Day. The day-bar path defaults to the hybrid -- sessions
where the calendar can vouch, ACT/365 past the horizon -- and stamps
``name_for`` on every landed row. The live path is still ACT/365 until step 4;
see ``docs/plans/trading-day-calendar.md``. This module makes the choice a
passed object instead of a module constant, so it can be measured against the
alternative and recorded per landed row.

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

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol, runtime_checkable

from ingest.common.market_gate import is_weekday
from pricing.calendar import CalendarRangeError, SessionCalendar, load_session_calendar
from pricing.conventions import CALENDAR_DAYS_PER_YEAR, TRADING_DAYS_PER_YEAR

# What a landed row may stamp. ``HybridSessions.name`` is "hybrid"; rows stamp
# ``name_for``, which is one of these two -- never "hybrid" -- so a reader can
# tell which convention actually produced ``t_years``.
ROW_DAYCOUNTS = frozenset({"act/365", "bus/252"})


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

    Kept selectable so the backtester can re-run history under either
    convention. The day-bar default is the hybrid; this is still what the
    hybrid falls back to past the calendar horizon, and what the live path
    uses until step 4.
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

    Used inside :class:`HybridSessions`. On its own it raises rather than
    guessing for a date its calendar cannot vouch for, which includes every
    expiry past the vendor's upcoming-holiday horizon -- that is why the
    default is the hybrid, not this convention by itself.
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
    means a weekday the calendar should have answered: a key missing
    *inside* attested history (corrupted coverage), or a weekday stranded
    *between* attested history and the forward window (a sync job that has
    stopped running). Both strike the short end, where a one-session error
    in T is largest. Swallowing either would downgrade the 5-45 DTE book to
    ACT/365 and call it a normal day, so those holes are re-raised: the one
    refusal ``SessionCalendar`` makes that nothing here should be able to
    paper over.
    """

    sessions: TradingSessions
    fallback: CalendarDays = ACT_365
    name: str = "hybrid"

    def _spans_coverage_hole(self, start: date, end: date) -> bool:
        """True when ``(start, end]`` contains a weekday the calendar should have answered.

        Two shapes, neither a shape of the book:

        * an interior hole -- a weekday inside attested history missing from
          the map.
        * a stale gap -- a weekday after attested history and before the
          forward window opens.

        Only a *weekday* counts: the healthy Saturday between the Saturday
        and Sunday jobs needs no coverage and must not strand the LEAPS tail
        that happens to span it. Before-history and after-horizon misses are
        the fallback, not this.
        """
        calendar = self.sessions.calendar
        if not calendar.sessions:
            return False
        attested_lo = min(calendar.sessions)
        attested_hi = max(calendar.sessions)
        # Holes live inside attested history or in the stale gap; past the
        # horizon is the fallback and is not scanned.
        hole_through = max(attested_hi, calendar.forward_from - timedelta(days=1))
        day = start + timedelta(days=1)
        through = min(end, hole_through)
        while day <= through:
            if is_weekday(day):
                if attested_lo <= day <= attested_hi and day not in calendar.sessions:
                    return True
                if attested_hi < day < calendar.forward_from:
                    return True
            day += timedelta(days=1)
        return False

    def _resolve(self, start: date, end: date) -> tuple[float, str]:
        try:
            return self.sessions.year_fraction(start, end), self.sessions.name
        except CalendarRangeError:
            if self._spans_coverage_hole(start, end):
                raise
            return self.fallback.year_fraction(start, end), self.fallback.name

    def year_fraction(self, start: date, end: date) -> float:
        return self._resolve(start, end)[0]

    def name_for(self, start: date, end: date) -> str:
        return self._resolve(start, end)[1]


class DayCountStampError(ValueError):
    """A landed row is missing its convention stamp, or the stamp is unknown."""


def require_daycount_stamps(
    rows: Sequence[Mapping[str, object]],
    *,
    context: str = "",
) -> list[str]:
    """Return each row's stamp, or raise if one is missing or unknown.

    Mixed ``act/365`` and ``bus/252`` in one batch is the hybrid's normal
    shape (LEAPS tail vs the rest) and is not an error. Interpreting an
    unstamped ``t_years`` under either convention would be.
    """
    prefix = f"{context}: " if context else ""
    stamps: list[str] = []
    for i, row in enumerate(rows):
        raw = row.get("daycount")
        if raw is None or str(raw).strip() == "":
            raise DayCountStampError(f"{prefix}row {i} has no daycount stamp")
        stamp = str(raw)
        if stamp not in ROW_DAYCOUNTS:
            raise DayCountStampError(
                f"{prefix}row {i} has unknown daycount {stamp!r}; "
                f"expected one of {sorted(ROW_DAYCOUNTS)}"
            )
        stamps.append(stamp)
    return stamps


_HYBRID_CACHE: dict[str, HybridSessions] = {}


def _resolve_data_root(data_root: str | os.PathLike[str] | None) -> str:
    if data_root is not None:
        return str(data_root)
    return os.environ.get("DATA_ROOT", "/data/massive")


def hybrid_for(data_root: str | os.PathLike[str] | None = None) -> HybridSessions:
    """The session-count convention over the box's own calendar files."""
    key = _resolve_data_root(data_root)
    cached = _HYBRID_CACHE.get(key)
    if cached is None:
        cached = HybridSessions(TradingSessions(load_session_calendar(data_root)))
        _HYBRID_CACHE[key] = cached
    return cached


@dataclass(frozen=True, slots=True)
class _DefaultHybrid:
    """Lazy hybrid over ``DATA_ROOT``. Importing this module must not need the warehouse.

    ``build_for_date`` binds :func:`hybrid_for` to ``settings.data_root`` so a
    staging warehouse is not priced on the box calendar. Tests that recover a
    synthetic ACT/365 chain must pass :data:`ACT_365` explicitly.
    """

    name: str = "hybrid"

    def year_fraction(self, start: date, end: date) -> float:
        return hybrid_for().year_fraction(start, end)

    def name_for(self, start: date, end: date) -> str:
        return hybrid_for().name_for(start, end)


# What every caller gets when it does not say. Changing this line changes every
# T in the day-bar path at once, which is why it is a line and not a literal.
DEFAULT_DAYCOUNT: DayCount = _DefaultHybrid()


def discount_year_fraction(start: date, end: date) -> float:
    """Money time, ACT/365: rate-curve tenors and discount factors.

    Never a session count, and clamped at zero so a same-day or past expiry
    resolves the front of the curve rather than reflecting off it.
    """
    return max((end - start).days, 0) / float(CALENDAR_DAYS_PER_YEAR)
