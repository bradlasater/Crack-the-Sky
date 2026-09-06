"""Exact pins for ACT/365 year fraction and per-root settlement instants.

T = (settlement_instant - asof) / (365 * 86400 seconds). Changing 365 to
365.25, 252, or treating expiry as UTC midnight must fail these.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from marketdata.opra import parse_opra
from pricing.from_market import expiry_instant, year_fraction

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
