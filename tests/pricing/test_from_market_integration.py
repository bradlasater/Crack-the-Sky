"""Integration behaviour for the glue layer, against this feed's actual shape.

Both regressions here were invisible to the unit tests because every synthetic
fixture has an `underlying_price` and a comfortably future expiry.
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from marketdata.opra import SETTLEMENT_ET, parse_opra, settlement_time_et
from marketdata.types import Forward, Quote
from pricing.bsm import price as bsm_price
from pricing.from_market import (
    expiry_instant,
    greeks_quote,
    implied_vol_quote,
    price_quote,
    year_fraction,
)

ET = dt.timezone(dt.timedelta(hours=-4))


def _quote(ticker: str, *, underlying_price: float | None, asof: dt.datetime,
           last: float | None = 10.0) -> Quote:
    return Quote(
        contract=parse_opra(ticker),
        last=last, day_close=None,
        underlying_price=underlying_price,
        asof_ns=int(asof.timestamp() * 1e9),
        open_interest=1000,
    )


def _forward(expiry: dt.date, fwd: float) -> Forward:
    return Forward(underlying="SPX", expiry=expiry, atm_strike=fwd, forward=fwd,
                   call_price=1.0, put_price=1.0, pairs=100,
                   asof_ns=0, method="parity")


# ---------------------------------------------------------------------------
# SPX has no spot on this tier
# ---------------------------------------------------------------------------

def test_spx_prices_from_the_parity_forward() -> None:
    """The regression: every SPX/SPXW quote used to raise.

    The index level is 403 on this tier and the snapshot carries
    underlying_price=null for the whole SPX chain -- 68% of the universe.
    """
    asof = dt.datetime(2026, 9, 1, 12, 0, tzinfo=ET)
    q = _quote("O:SPXW260918C07700000", underlying_price=None, asof=asof, last=120.0)
    f = _forward(dt.date(2026, 9, 18), 7691.0)

    px = price_quote(q, r=0.04, sigma=0.15, forward=f)
    assert px > 0
    g = greeks_quote(q, r=0.04, sigma=0.15, forward=f)
    assert 0.0 < g.delta < 1.0
    assert g.vega > 0
    iv = implied_vol_quote(q, r=0.04, forward=f)
    assert 0.0 < iv < 5.0


def test_forward_fallback_is_black76() -> None:
    """S = F e^{-rT} with q=0 must reproduce e^{-rT}[F N(d1) - K N(d2)]."""
    asof = dt.datetime(2026, 9, 1, 12, 0, tzinfo=ET)
    K, F_, r, sigma = 7700.0, 7691.0, 0.04, 0.15
    q = _quote("O:SPXW260918C07700000", underlying_price=None, asof=asof)
    fwd = _forward(dt.date(2026, 9, 18), F_)

    got = price_quote(q, r=r, sigma=sigma, forward=fwd)
    T = year_fraction(q.contract, q.asof_ns)
    expected = bsm_price(F_ * math.exp(-r * T), K, T, r, sigma, "call", q=0.0)
    assert got == pytest.approx(expected, rel=1e-12)


def test_missing_spot_and_no_forward_fails_loudly() -> None:
    asof = dt.datetime(2026, 9, 1, 12, 0, tzinfo=ET)
    q = _quote("O:SPXW260918C07700000", underlying_price=None, asof=asof)
    with pytest.raises(ValueError, match="not entitled|underlying_price"):
        price_quote(q, r=0.04, sigma=0.15)


def test_mismatched_forward_expiry_is_rejected() -> None:
    asof = dt.datetime(2026, 9, 1, 12, 0, tzinfo=ET)
    q = _quote("O:SPXW260918C07700000", underlying_price=None, asof=asof)
    with pytest.raises(ValueError, match="expiry"):
        price_quote(q, r=0.04, sigma=0.15, forward=_forward(dt.date(2026, 9, 25), 7691.0))


def test_spot_still_wins_when_present() -> None:
    """SPY has a real underlying_price; the forward must not override it."""
    asof = dt.datetime(2026, 9, 1, 12, 0, tzinfo=ET)
    q = _quote("O:SPY260918C00770000", underlying_price=767.38, asof=asof)
    assert price_quote(q, r=0.04, sigma=0.15) > 0


# ---------------------------------------------------------------------------
# Expiry is an instant, not a date
# ---------------------------------------------------------------------------

def test_settlement_times_are_per_root() -> None:
    assert settlement_time_et("SPY") == (16, 0)
    assert settlement_time_et("SPXW") == (16, 0)
    assert settlement_time_et("SPX") == (9, 30)      # AM settled
    # VIX options settle to the SOQ at the Wednesday OPEN -- both series are
    # AM settled, unlike the SPX/SPXW split.
    assert settlement_time_et("VIX") == (9, 30)
    assert settlement_time_et("VIXW") == (9, 30)
    assert set(SETTLEMENT_ET) == {"SPY", "SPX", "SPXW", "VIX", "VIXW"}


def test_expiry_instant_is_settlement_not_utc_midnight() -> None:
    c = parse_opra("O:SPXW260918C07700000")
    inst = expiry_instant(c)
    assert inst == dt.datetime(2026, 9, 18, 20, 0, tzinfo=dt.UTC)  # 16:00 ET
    assert inst.hour != 0, "UTC midnight understates T by ~20h at every tenor"


def test_am_settled_spx_expires_earlier_than_pm_weekly() -> None:
    am = expiry_instant(parse_opra("O:SPX260918C07700000"))
    pm = expiry_instant(parse_opra("O:SPXW260918C07700000"))
    assert am < pm
    assert (pm - am) == dt.timedelta(hours=6, minutes=30)


def test_zero_dte_is_priceable_before_the_close() -> None:
    """The old UTC-midnight convention expired same-day options the night before."""
    c = parse_opra("O:SPXW260918C07700000")
    morning = dt.datetime(2026, 9, 18, 10, 0, tzinfo=ET)
    t = year_fraction(c, int(morning.timestamp() * 1e9))
    assert t > 0
    assert t == pytest.approx(6.0 / (24 * 365), rel=1e-6)   # 10:00 -> 16:00 ET


def test_after_settlement_still_raises() -> None:
    c = parse_opra("O:SPXW260918C07700000")
    evening = dt.datetime(2026, 9, 18, 17, 0, tzinfo=ET)
    with pytest.raises(ValueError, match="non-positive T"):
        year_fraction(c, int(evening.timestamp() * 1e9))


def test_year_fraction_is_longer_than_the_utc_midnight_convention() -> None:
    """Quantifies the bias the old convention introduced."""
    c = parse_opra("O:SPXW260918C07700000")
    asof = dt.datetime(2026, 9, 11, 16, 0, tzinfo=ET)      # 7 DTE
    asof_ns = int(asof.timestamp() * 1e9)
    correct = year_fraction(c, asof_ns)
    midnight = (dt.datetime(2026, 9, 18, tzinfo=dt.UTC).timestamp() * 1e9 - asof_ns) / (
        365 * 86400.0 * 1e9
    )
    assert correct > midnight
    assert (correct - midnight) * 365 * 24 == pytest.approx(20.0, abs=0.01)  # ~20 hours
