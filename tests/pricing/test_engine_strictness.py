"""CRR must refuse to price rather than return a plausible wrong number."""

from __future__ import annotations

import datetime as dt

import pytest

from marketdata.opra import parse_opra
from marketdata.types import Quote
from pricing.engine import AmericanCRR, EuropeanBSM, crr_price
from pricing.from_market import engine_for, greeks_quote, price_quote

# ---------------------------------------------------------------------------
# Arbitrage-free condition
# ---------------------------------------------------------------------------

def test_crr_rejects_an_out_of_range_risk_neutral_probability() -> None:
    """d < e^{(r-q)dt} < u must hold; clipping p silently changes the drift."""
    with pytest.raises(ValueError, match="outside \\[0, 1\\]|not arbitrage-free"):
        crr_price(100.0, 100.0, 1.0, r=5.0, sigma=0.01, q=0.0,
                  call_put="call", n_steps=4)


def test_crr_error_names_the_remedy() -> None:
    with pytest.raises(ValueError) as exc:
        crr_price(100.0, 100.0, 1.0, r=5.0, sigma=0.01, q=0.0,
                  call_put="call", n_steps=4)
    assert "n_steps" in str(exc.value)


def test_more_steps_restore_the_condition() -> None:
    """The arbitrage-free condition is |r-q|*sqrt(dt) < sigma, so shrinking dt
    fixes a borderline tree. r=0.5, sigma=0.05, T=1 needs n > 100."""
    with pytest.raises(ValueError):
        crr_price(100.0, 100.0, 1.0, r=0.5, sigma=0.05, q=0.0,
                  call_put="call", n_steps=50)
    px = crr_price(100.0, 100.0, 1.0, r=0.5, sigma=0.05, q=0.0,
                   call_put="call", n_steps=801)
    assert px > 0


# ---------------------------------------------------------------------------
# Strict call/put
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["invalid", "kall", "", "x", "cat"])
def test_crr_rejects_unknown_call_put(bad: str) -> None:
    """`startswith("c")` read anything else as a put and priced it."""
    with pytest.raises(ValueError, match="call/put"):
        crr_price(100.0, 100.0, 1.0, r=0.05, sigma=0.2, q=0.0, call_put=bad)


def test_cat_is_not_a_call() -> None:
    """'cat' used to start with 'c' and price as a call."""
    with pytest.raises(ValueError):
        crr_price(100.0, 100.0, 1.0, r=0.05, sigma=0.2, q=0.0, call_put="cat")


@pytest.mark.parametrize("good", ["call", "put", "C", "P", "c", "p"])
def test_crr_accepts_the_documented_spellings(good: str) -> None:
    assert crr_price(100.0, 100.0, 1.0, r=0.05, sigma=0.2, q=0.0,
                     call_put=good) > 0


def test_american_crr_methods_are_strict_too() -> None:
    eng = AmericanCRR()
    with pytest.raises(ValueError, match="call/put"):
        eng.price(100.0, 100.0, 1.0, 0.05, 0.2, "invalid")
    with pytest.raises(ValueError, match="call/put"):
        eng.greeks(100.0, 100.0, 1.0, 0.05, 0.2, "invalid")


# ---------------------------------------------------------------------------
# Engine dispatch follows the contract, not a module default
# ---------------------------------------------------------------------------

def test_engine_is_chosen_by_exercise_style() -> None:
    assert isinstance(engine_for(parse_opra("O:SPY260918C00770000")), AmericanCRR)
    assert isinstance(engine_for(parse_opra("O:SPXW260918C07700000")), EuropeanBSM)
    assert isinstance(engine_for(parse_opra("O:SPX260918C07700000")), EuropeanBSM)


def _spy_quote() -> Quote:
    asof = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=-4)))
    return Quote(
        contract=parse_opra("O:SPY260918P00770000"),
        last=12.0, day_close=None, underlying_price=767.38,
        asof_ns=int(asof.timestamp() * 1e9), open_interest=1000,
    )


def test_spy_put_is_valued_american_by_default() -> None:
    """SPY is American; valuing it European drops the early-exercise premium."""
    q = _spy_quote()
    american = price_quote(q, r=0.05, sigma=0.25, q=0.02)
    european = price_quote(q, r=0.05, sigma=0.25, q=0.02, engine=EuropeanBSM())
    assert american >= european
    assert american == pytest.approx(european, rel=0.5)   # same ballpark


def test_explicit_engine_still_overrides() -> None:
    q = _spy_quote()
    assert price_quote(q, r=0.05, sigma=0.25, engine=EuropeanBSM()) > 0
    assert greeks_quote(q, r=0.05, sigma=0.25, engine=EuropeanBSM()).vega > 0
