"""Day-count conventions, and the vol-time / money-time split they encode.

Nothing here changes a number yet: ACT/365 is still the default everywhere,
and the pins below are what will make flipping it a deliberate act rather than
a silent one. The load-bearing tests are the two at the bottom -- a passed
convention must move vol time and must *not* move the rate tenor.
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from pricing import surface as sf
from pricing import term_structure as ts
from pricing.bsm import price
from pricing.calendar import CalendarRangeError, SessionCalendar
from pricing.daycount import (
    ACT_365,
    DEFAULT_DAYCOUNT,
    CalendarDays,
    DayCount,
    HybridSessions,
    TradingSessions,
    discount_year_fraction,
)

DAY = date(2026, 8, 28)
EXPIRY = date(2026, 9, 25)
DTE = (EXPIRY - DAY).days  # 28 calendar days
SESSIONS = 19  # sessions in (DAY, EXPIRY] -- Labor Day and four weekends out
R = 0.04
F = 7700.0
VOL = 0.18


# session_calendar lives in tests/pricing/conftest.py.


def _sym(root: str, expiry: date, kind: str, strike: float) -> str:
    return (f"O:{root}{expiry:%y%m%d}{'C' if kind == 'call' else 'P'}"
            f"{int(round(strike * 1000)):08d}")


def _chain_bars() -> list[dict]:
    """Day bars for a mildly skewed chain in the forward measure.

    Wide enough to clear the surface fit's MIN_STRIKES, and skewed so the SVI
    solve is not degenerate. Priced at ACT/365 throughout -- what the fits
    recover from it is beside the point here; only the T they stamp is.
    """
    t = DTE / 365.0
    bars = []
    for k in (float(x) for x in range(7300, 8101, 25)):
        vol = VOL - 0.15 * math.log(k / F)
        for kind in ("call", "put"):
            bars.append({"ticker": _sym("SPXW", EXPIRY, kind, k),
                         "close": float(price(F, k, t, R, vol, kind, q=R)),
                         "window_end_ns": 1})
    return bars


# ---------------------------------------------------------------------------
# The conventions themselves
# ---------------------------------------------------------------------------


def test_calendar_days_is_exactly_act_365() -> None:
    got = ACT_365.year_fraction(DAY, EXPIRY)
    assert got == pytest.approx(DTE / 365.0, rel=1e-12)
    assert got != pytest.approx(DTE / 365.25, rel=1e-9)
    assert got != pytest.approx(DTE / 252.0, rel=1e-9)
    assert ACT_365.name == "act/365"


def test_default_is_still_act_365() -> None:
    """The pin that makes step 3 a decision. Changing the default moves every
    T on the day-bar path at once, so it should fail a test on the way."""
    assert DEFAULT_DAYCOUNT is ACT_365
    assert isinstance(DEFAULT_DAYCOUNT, CalendarDays)
    assert DEFAULT_DAYCOUNT.days_per_year == 365.0


def test_trading_sessions_counts_sessions_over_252(
    session_calendar: SessionCalendar,
) -> None:
    bus = TradingSessions(session_calendar)
    assert session_calendar.sessions_between(DAY, EXPIRY) == SESSIONS
    assert bus.year_fraction(DAY, EXPIRY) == pytest.approx(SESSIONS / 252.0, rel=1e-12)
    assert bus.name == "bus/252"


def test_trading_sessions_refuses_past_the_horizon(
    session_calendar: SessionCalendar,
) -> None:
    """Every LEAPS expiry lands here, which is why adopting this convention is
    a decision about the long end of the book and not only about T."""
    with pytest.raises(CalendarRangeError):
        TradingSessions(session_calendar).year_fraction(DAY, date(2031, 12, 19))


# ---------------------------------------------------------------------------
# Money time
# ---------------------------------------------------------------------------


def test_discount_year_fraction_is_act_365() -> None:
    assert discount_year_fraction(DAY, EXPIRY) == pytest.approx(DTE / 365.0, rel=1e-12)


def test_discount_year_fraction_clamps_at_zero() -> None:
    """A past or same-day expiry resolves the front of the curve rather than
    reflecting off it into a negative tenor."""
    assert discount_year_fraction(DAY, DAY) == 0.0
    assert discount_year_fraction(EXPIRY, DAY) == 0.0


# ---------------------------------------------------------------------------
# Zero-session spans (finding 8)
# ---------------------------------------------------------------------------

# 2025-01-09 was a full-day close -- the national day of mourning for
# President Carter -- and it is the expiry of the only zero-session spans in
# the archive. Wednesday to Thursday: one calendar day, no sessions.
MOURNING_DAY = date(2025, 1, 9)
MOURNING_EVE = date(2025, 1, 8)


def _one_day_chain() -> list[dict]:
    """A chain expiring the day after ``MOURNING_EVE``, priced at ACT/365."""
    t = 1.0 / 365.0
    bars = []
    for k in (float(x) for x in range(7300, 8101, 25)):
        vol = VOL - 0.15 * math.log(k / F)
        for kind in ("call", "put"):
            bars.append({"ticker": _sym("SPXW", MOURNING_DAY, kind, k),
                         "close": float(price(F, k, t, R, vol, kind, q=R)),
                         "window_end_ns": 1})
    return bars


def _built(builder: str, **kw) -> int:
    """How many rows / slices the builder produced for the one-day chain."""
    if builder == "term_structure":
        return len(ts.build_rows(_one_day_chain(), MOURNING_EVE,
                                 roots=("SPXW",), rate_fn=lambda _a, _t: R, **kw))
    surfaces = sf.build_surfaces(_one_day_chain(), MOURNING_EVE,
                                 roots=("SPXW",), rate_fn=lambda _a, _t: R, **kw)
    return sum(len(s.slices) for s in surfaces.values())


def test_a_closure_only_span_counts_zero_sessions(
    session_calendar: SessionCalendar,
) -> None:
    """The premise: dte is 1, but no session falls in the span."""
    assert (MOURNING_DAY - MOURNING_EVE).days == 1
    assert session_calendar.is_session(MOURNING_DAY) is False
    assert session_calendar.sessions_between(MOURNING_EVE, MOURNING_DAY) == 0
    assert TradingSessions(session_calendar).year_fraction(
        MOURNING_EVE, MOURNING_DAY) == 0.0


@pytest.mark.parametrize("builder", ["term_structure", "surface"])
def test_zero_vol_time_rows_are_skipped_not_landed(
    session_calendar: SessionCalendar, builder: str
) -> None:
    """A row whose IV cannot exist must not be written.

    Without the guard the span survives the ``dte <= 0`` check, T reaches the
    solver as 0.0, every inversion raises and is swallowed into None, and the
    row lands with a null IV -- the silent shape this whole change exists to
    prevent. Landing it would also stamp ``t_years = 0``, a division by zero
    waiting in every downstream greek.
    """
    bus = TradingSessions(session_calendar)
    assert _built(builder, daycount=bus) == 0


@pytest.mark.parametrize("builder", ["term_structure", "surface"])
def test_the_same_span_still_lands_under_act_365(builder: str) -> None:
    """The guard is about vol time, not about the date.

    Under the default convention the span is 1/365 and the row is perfectly
    ordinary, so the skip must not fire -- otherwise this would be a silent
    behaviour change to the landed archive rather than a guard on a
    convention that is not switched on yet.
    """
    assert _built(builder) == 1
    row = ts.build_rows(_one_day_chain(), MOURNING_EVE, roots=("SPXW",),
                        rate_fn=lambda _a, _t: R)[0]
    assert row["t_years"] == pytest.approx(1.0 / 365.0, rel=1e-12)


# ---------------------------------------------------------------------------
# The seam, on the day-bar path
# ---------------------------------------------------------------------------


def _rate_probe():
    """A flat rate_fn that records every tenor it is asked for."""
    seen: list[float] = []

    def rate_fn(_as_of, T):  # noqa: ANN001
        seen.append(T)
        return R

    return rate_fn, seen


def test_default_build_is_unchanged_act_365() -> None:
    row = ts.build_rows(_chain_bars(), DAY, roots=("SPXW",),
                        rate_fn=lambda _a, _t: R)[0]
    assert row["t_years"] == pytest.approx(DTE / 365.0, rel=1e-12)


@pytest.mark.parametrize("builder", ["term_structure", "surface"])
def test_a_passed_convention_moves_vol_time(
    session_calendar: SessionCalendar, builder: str
) -> None:
    bus = TradingSessions(session_calendar)
    if builder == "term_structure":
        t_years = ts.build_rows(_chain_bars(), DAY, roots=("SPXW",),
                                rate_fn=lambda _a, _t: R, daycount=bus)[0]["t_years"]
    else:
        surfaces = sf.build_surfaces(_chain_bars(), DAY, roots=("SPXW",),
                                     rate_fn=lambda _a, _t: R, daycount=bus)
        t_years = surfaces["SPXW"].slices[0].t_years
    assert t_years == pytest.approx(SESSIONS / 252.0, rel=1e-12)
    assert t_years != pytest.approx(DTE / 365.0, rel=1e-6)


@pytest.mark.parametrize("builder", ["term_structure", "surface"])
def test_a_passed_convention_does_not_move_money_time(
    session_calendar: SessionCalendar, builder: str
) -> None:
    """The one that matters. Interest accrues on weekends, so the rate tenor
    stays ACT/365 however vol time is counted -- converting it would both
    shorten the discount factor and read the wrong point off the Treasury
    curve, and neither error shows up in the output.
    """
    bus = TradingSessions(session_calendar)
    rate_fn, seen = _rate_probe()
    if builder == "term_structure":
        ts.build_rows(_chain_bars(), DAY, roots=("SPXW",),
                      rate_fn=rate_fn, daycount=bus)
    else:
        sf.build_surfaces(_chain_bars(), DAY, roots=("SPXW",),
                          rate_fn=rate_fn, daycount=bus)
    assert seen, "rate_fn was never called"
    assert all(t == pytest.approx(DTE / 365.0, rel=1e-12) for t in seen)
    assert all(t != pytest.approx(SESSIONS / 252.0, rel=1e-6) for t in seen)


# ---------------------------------------------------------------------------
# The hybrid: sessions where the calendar can vouch, ACT/365 beyond
# ---------------------------------------------------------------------------


def test_hybrid_uses_sessions_inside_the_horizon(
    session_calendar: SessionCalendar,
) -> None:
    h = HybridSessions(TradingSessions(session_calendar))
    assert h.year_fraction(DAY, EXPIRY) == pytest.approx(SESSIONS / 252.0, rel=1e-12)
    assert h.name_for(DAY, EXPIRY) == "bus/252"


def test_hybrid_falls_back_beyond_the_horizon(
    session_calendar: SessionCalendar,
) -> None:
    """A LEAPS expiry keeps the convention it already had rather than being
    refused -- and says so, so the two kinds of row stay distinguishable."""
    leaps = date(2031, 12, 19)
    h = HybridSessions(TradingSessions(session_calendar))
    assert h.year_fraction(DAY, leaps) == pytest.approx(
        ACT_365.year_fraction(DAY, leaps), rel=1e-12)
    assert h.name_for(DAY, leaps) == "act/365"


def test_hybrid_falls_back_before_attested_history(
    session_calendar: SessionCalendar,
) -> None:
    old = date(2019, 3, 14)
    h = HybridSessions(TradingSessions(session_calendar))
    assert h.year_fraction(old, date(2019, 4, 18)) == pytest.approx(35 / 365.0, rel=1e-12)
    assert h.name_for(old, date(2019, 4, 18)) == "act/365"


@pytest.fixture
def stale_calendar(session_calendar: SessionCalendar) -> SessionCalendar:
    """``history_audit`` a week behind, so weekdays are stranded in the gap.

    Attested history stops 2026-08-27 while the holiday window still opens
    2026-09-06, leaving 2026-08-28 through 2026-09-04 -- six weekdays -- that
    no source reaches. Mirrors ``test_weekday_in_the_gap_raises``.
    """
    return SessionCalendar(
        sessions={d: v for d, v in session_calendar.sessions.items()
                  if d < date(2026, 8, 28)},
        holidays=session_calendar.holidays,
        early_closes=session_calendar.early_closes,
        forward_from=session_calendar.forward_from,
        forward_through=session_calendar.forward_through,
    )


def test_hybrid_re_raises_a_stale_calendar_gap(stale_calendar: SessionCalendar) -> None:
    """A stale sync job is not a shape of the book, and must not be answered.

    This is the case the fallback must not swallow: the span is short-dated,
    well inside the horizon, and ACT/365 would come back looking like every
    other row. Refusing is the whole point of ``SessionCalendar`` raising.
    """
    h = HybridSessions(TradingSessions(stale_calendar))
    with pytest.raises(CalendarRangeError, match="not covered"):
        h.year_fraction(date(2026, 8, 27), date(2026, 9, 18))
    with pytest.raises(CalendarRangeError, match="not covered"):
        h.name_for(date(2026, 8, 27), date(2026, 9, 18))


def test_hybrid_still_refuses_a_leaps_span_across_a_stale_gap(
    stale_calendar: SessionCalendar,
) -> None:
    """Past the horizon *and* across a stale gap: the gap wins.

    Falling back here would return a plausible ACT/365 number and lose the
    only signal that a job stopped running, so the operational failure is
    reported ahead of the convention question.
    """
    h = HybridSessions(TradingSessions(stale_calendar))
    with pytest.raises(CalendarRangeError):
        h.year_fraction(date(2026, 8, 27), date(2031, 12, 19))


def test_hybrid_falls_back_across_the_healthy_weekend_gap(
    session_calendar: SessionCalendar,
) -> None:
    """The steady-state gap is one Saturday, and must not look stale.

    ``history_audit`` verifies through Friday and ``holidays_sync`` opens its
    window on Sunday, so 2026-09-05 is uncovered on a perfectly healthy box --
    it just needs no coverage. A LEAPS span crossing it still falls back on
    the horizon, which is the only reason it was refused.
    """
    assert date(2026, 9, 5).weekday() == 5
    h = HybridSessions(TradingSessions(session_calendar))
    leaps = date(2031, 12, 19)
    assert h.name_for(date(2026, 9, 4), leaps) == "act/365"
    assert h.year_fraction(date(2026, 9, 4), leaps) == pytest.approx(
        ACT_365.year_fraction(date(2026, 9, 4), leaps), rel=1e-12)


def test_simple_conventions_report_their_own_name() -> None:
    """``name_for`` is what a row stamps; for a convention that never falls
    back it is just ``name``, on every pair."""
    assert ACT_365.name_for(DAY, EXPIRY) == ACT_365.name == "act/365"
    assert ACT_365.name_for(date(1999, 1, 1), date(2099, 1, 1)) == "act/365"


def test_hybrid_satisfies_the_protocol(session_calendar: SessionCalendar) -> None:
    for conv in (ACT_365, TradingSessions(session_calendar),
                 HybridSessions(TradingSessions(session_calendar))):
        assert isinstance(conv, DayCount)
