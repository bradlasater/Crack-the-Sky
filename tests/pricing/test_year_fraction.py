"""Exact pins for ACT/365 year fraction and per-root settlement instants.

T = (settlement_instant - asof) / (365 * 86400 seconds). Changing 365 to
365.25, 252, or treating expiry as UTC midnight must fail these.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from marketdata.opra import parse_opra
from pricing.calendar import CalendarRangeError, SessionCalendar
from pricing.from_market import expiry_instant, year_fraction
from tests.conftest import load_fixture

ET = ZoneInfo("America/New_York")
SECONDS_PER_DAY = 86400.0
DAYS_PER_YEAR = 365


@pytest.mark.parametrize(
    "ticker",
    [
        "O:SPY260918C00770000",
        "O:SPXW260918C07700000",
        "O:SPX260918C07700000",
        "O:VIX260916C00020000",
        "O:VIXW260916P00016000",
    ],
)
def test_year_fraction_is_act_365_seconds_ratio(ticker: str) -> None:
    contract = parse_opra(ticker)
    asof = datetime(2026, 9, 11, 10, 0, tzinfo=ET)
    asof_ns = int(asof.timestamp() * 1e9)
    got = year_fraction(contract, asof_ns)
    delta_s = (expiry_instant(contract) - asof).total_seconds()
    expected = delta_s / (DAYS_PER_YEAR * SECONDS_PER_DAY)
    assert got == pytest.approx(expected, rel=1e-12)
    assert got != pytest.approx(delta_s / (365.25 * SECONDS_PER_DAY), rel=1e-9)
    assert got != pytest.approx(delta_s / (252 * SECONDS_PER_DAY), rel=1e-9)


def test_spy_and_spxw_settle_at_1600_et() -> None:
    spy = expiry_instant(parse_opra("O:SPY260918C00770000"))
    spxw = expiry_instant(parse_opra("O:SPXW260918C07700000"))
    # 16:00 ET in September is EDT (UTC-4) → 20:00 UTC.
    assert spy == datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
    assert spxw == datetime(2026, 9, 18, 20, 0, tzinfo=UTC)


def test_spx_and_vix_settle_at_0930_et() -> None:
    spx = expiry_instant(parse_opra("O:SPX260918C07700000"))
    vix = expiry_instant(parse_opra("O:VIX260916C00020000"))
    vixw = expiry_instant(parse_opra("O:VIXW260916P00016000"))
    # 09:30 ET in September is EDT (UTC-4) → 13:30 UTC.
    assert spx == datetime(2026, 9, 18, 13, 30, tzinfo=UTC)
    assert vix == datetime(2026, 9, 16, 13, 30, tzinfo=UTC)
    assert vixw == datetime(2026, 9, 16, 13, 30, tzinfo=UTC)


def test_winter_settlement_follows_est_not_a_fixed_utc_offset() -> None:
    """A hardcoded UTC-4 would place 16:00 ET at 20:00 UTC year-round."""
    spy = expiry_instant(parse_opra("O:SPY260116C00700000"))
    spx = expiry_instant(parse_opra("O:SPX260116C07700000"))
    assert spy == datetime(2026, 1, 16, 21, 0, tzinfo=UTC)     # 16:00 EST
    assert spx == datetime(2026, 1, 16, 14, 30, tzinfo=UTC)    # 09:30 EST


def test_year_fraction_at_and_after_settlement_raises() -> None:
    c = parse_opra("O:SPXW260918C07700000")
    settle_ns = int(expiry_instant(c).timestamp() * 1e9)
    with pytest.raises(ValueError, match="non-positive T"):
        year_fraction(c, settle_ns)
    with pytest.raises(ValueError, match="non-positive T"):
        year_fraction(c, settle_ns + 1)


def test_intraday_zero_dte_matches_the_remaining_seconds() -> None:
    """10:00 ET → 16:00 ET is 6 hours; T = 6h / (365 * 86400)."""
    c = parse_opra("O:SPXW260918C07700000")
    asof = datetime(2026, 9, 18, 10, 0, tzinfo=ET)
    got = year_fraction(c, int(asof.timestamp() * 1e9))
    expected = (6.0 * 3600.0) / (DAYS_PER_YEAR * SECONDS_PER_DAY)
    assert got == pytest.approx(expected, rel=1e-12)


# ---------------------------------------------------------------------------
# Half days
#
# A half day closes at 13:00 ET and opens at the usual 09:30, so it can only
# reach a PM settlement. The box's window carries exactly two -- Black Friday
# 2026-11-27 and Christmas Eve 2026-12-24 -- and both are real SPY/SPXW expiry
# dates. Both fall in EST, so 13:00 ET is 18:00 UTC and 16:00 ET is 21:00 UTC.
# ---------------------------------------------------------------------------

BLACK_FRIDAY = "261127"
CHRISTMAS_EVE = "261224"
EARLY_CLOSE_UTC = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
FULL_CLOSE_UTC = datetime(2026, 11, 27, 21, 0, tzinfo=UTC)


@pytest.mark.parametrize("root", ["SPY", "SPXW"])
def test_pm_expiry_on_a_half_day_settles_at_the_early_close(
    root: str, session_calendar: SessionCalendar
) -> None:
    c = parse_opra(f"O:{root}{BLACK_FRIDAY}C00700000")
    assert expiry_instant(c, session_calendar) == EARLY_CLOSE_UTC
    xmas = parse_opra(f"O:{root}{CHRISTMAS_EVE}C00700000")
    assert expiry_instant(xmas, session_calendar) == datetime(
        2026, 12, 24, 18, 0, tzinfo=UTC
    )


def test_without_a_calendar_a_half_day_keeps_the_full_session_close(
    session_calendar: SessionCalendar,
) -> None:
    """The defect this fix exists for: three hours of T that were never traded."""
    c = parse_opra(f"O:SPY{BLACK_FRIDAY}C00700000")
    assert expiry_instant(c) == FULL_CLOSE_UTC
    assert expiry_instant(c) - expiry_instant(c, session_calendar) == timedelta(hours=3)


@pytest.mark.parametrize("root", ["SPX", "VIX", "VIXW"])
def test_am_expiry_on_a_half_day_keeps_the_normal_open(
    root: str, session_calendar: SessionCalendar
) -> None:
    """An early close moves the close, not the open, so AM settlement stands."""
    c = parse_opra(f"O:{root}{BLACK_FRIDAY}C00700000")
    assert expiry_instant(c, session_calendar) == datetime(
        2026, 11, 27, 14, 30, tzinfo=UTC
    )
    assert expiry_instant(c, session_calendar) == expiry_instant(c)


@pytest.mark.parametrize(
    "ticker",
    ["O:SPY260918C00770000", "O:SPXW260918C07700000", "O:SPX260918C07700000"],
)
def test_a_full_session_is_unaffected_by_the_calendar(
    ticker: str, session_calendar: SessionCalendar
) -> None:
    c = parse_opra(ticker)
    assert expiry_instant(c, session_calendar) == expiry_instant(c)


def test_a_pm_expiry_past_the_holiday_window_raises(
    session_calendar: SessionCalendar,
) -> None:
    """Half days past the window are unrecorded, and 16:00 there would be a guess."""
    c = parse_opra("O:SPY270917C00700000")  # 2027-09-17, past 2027-07-05
    with pytest.raises(CalendarRangeError, match="outside the holiday window"):
        expiry_instant(c, session_calendar)


def test_an_am_expiry_past_the_holiday_window_does_not_consult_the_calendar(
    session_calendar: SessionCalendar,
) -> None:
    """AM settlement cannot move, so the unanswerable question is never asked."""
    c = parse_opra("O:SPX270917C07700000")
    assert expiry_instant(c, session_calendar) == datetime(
        2027, 9, 17, 13, 30, tzinfo=UTC
    )  # 09:30 EDT


def test_year_fraction_expires_a_pm_contract_at_the_early_close(
    session_calendar: SessionCalendar,
) -> None:
    """14:00 ET on a half day is after settlement, not two hours before it."""
    c = parse_opra(f"O:SPY{BLACK_FRIDAY}C00700000")
    asof_ns = int(datetime(2026, 11, 27, 14, 0, tzinfo=ET).timestamp() * 1e9)
    with pytest.raises(ValueError, match="non-positive T"):
        year_fraction(c, asof_ns, calendar=session_calendar)
    # Without the calendar the same contract still prices as live -- the bug.
    assert year_fraction(c, asof_ns) == pytest.approx(
        (2.0 * 3600.0) / (DAYS_PER_YEAR * SECONDS_PER_DAY), rel=1e-12
    )


def test_the_early_close_constant_matches_the_vendors_own_close_field(
    session_calendar: SessionCalendar,
) -> None:
    """13:00 ET is hardcoded because load_early_closes drops the time; the
    vendor still publishes it, so pin the two together."""
    records = load_fixture("holidays_upcoming_2026.json")
    closes = {
        r["date"]: r["close"] for r in records if r.get("status") == "early-close"
    }
    assert closes, "fixture carries no early-close record to check against"
    for day, close in closes.items():
        vendor = datetime.fromisoformat(close.replace("Z", "+00:00"))
        c = parse_opra(f"O:SPY{day[2:].replace('-', '')}C00700000")
        assert expiry_instant(c, session_calendar) == vendor
