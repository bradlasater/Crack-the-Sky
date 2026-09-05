"""American IV inversion: round-trip, fail-loud, never NaN.

The European suite is in ``test_iv.py``; this covers what early exercise
changes -- the bounds, the CRR-only vol floor, and the fact that a price at
intrinsic carries no volatility at all.
"""

from __future__ import annotations

import itertools
import math

import pytest

from pricing.bsm import price as bsm_price
from pricing.engine import crr_price
from pricing.iv import (
    american_bounds,
    crr_vol_floor,
    implied_vol,
    implied_vol_american,
)

S0 = 100.0
R = 0.05
STEPS = 401


def _amer(K: float, T: float, sigma: float, cp: str, *, q: float = 0.0,
          n_steps: int = STEPS) -> float:
    return crr_price(S0, K, T, R, sigma, cp, q=q, n_steps=n_steps, american=True)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

GRID = list(itertools.product([0.85, 0.95, 1.0, 1.05, 1.15], [7, 30, 180],
                              [0.10, 0.25, 0.60]))


@pytest.mark.parametrize("moneyness,dte,sigma", GRID)
@pytest.mark.parametrize("call_put", ["call", "put"])
def test_round_trip_recovers_sigma(moneyness: float, dte: int, sigma: float,
                                   call_put: str) -> None:
    """Price at sigma, invert, get sigma back -- wherever a price can say so.

    Skips the strikes whose price sits on the intrinsic floor: an American
    option optimally exercised now is worth the same at every sigma, so there
    is nothing to recover. That case is asserted directly further down.
    """
    K = S0 * moneyness
    T = dte / 365.0
    px = _amer(K, T, sigma, call_put)
    lower, _ = american_bounds(S0, K, T, R, call_put, q=0.0)
    if px - lower <= 1e-6:
        pytest.skip("no time value at this strike; not invertible at any sigma")
    got = implied_vol_american(px, S0, K, T, R, call_put, q=0.0, n_steps=STEPS)
    assert got == pytest.approx(sigma, abs=1e-5)


def test_round_trip_holds_with_a_dividend_yield() -> None:
    """q > 0 is what makes an American *call* worth exercising early."""
    K, T, sigma = 100.0, 0.75, 0.28
    px = _amer(K, T, sigma, "call", q=0.03)
    got = implied_vol_american(px, S0, K, T, R, "call", q=0.03, n_steps=STEPS)
    assert got == pytest.approx(sigma, abs=1e-5)


# ---------------------------------------------------------------------------
# Cross-checks against the European solver
# ---------------------------------------------------------------------------


def test_american_call_without_dividends_matches_european_iv() -> None:
    """An American call with q=0 is never exercised early, so the two agree.

    The sharpest available reference: it pins the American solver against the
    independent closed-form inversion rather than against itself.
    """
    K, T, sigma = 100.0, 0.5, 0.20
    px = _amer(K, T, sigma, "call", q=0.0, n_steps=801)
    amer = implied_vol_american(px, S0, K, T, R, "call", q=0.0, n_steps=801)
    euro = implied_vol(px, S0, K, T, R, "call", q=0.0)
    assert amer == pytest.approx(euro, abs=1e-3)
    assert amer == pytest.approx(sigma, abs=1e-5)


def test_european_invert_of_an_american_put_overstates_sigma() -> None:
    """The bug this solver exists to fix, stated as a test.

    An American put is worth more than the European one at the same sigma, so
    feeding its price to the European inverter buys the early-exercise premium
    with volatility that is not there.
    """
    K, T, sigma = 100.0, 1.0, 0.20
    px = _amer(K, T, sigma, "put", q=0.0, n_steps=801)
    assert px > bsm_price(S0, K, T, R, sigma, "put", q=0.0)

    amer = implied_vol_american(px, S0, K, T, R, "put", q=0.0, n_steps=801)
    euro = implied_vol(px, S0, K, T, R, "put", q=0.0)
    assert amer == pytest.approx(sigma, abs=1e-5)
    assert euro > amer + 0.005


# ---------------------------------------------------------------------------
# The tree is part of the answer
# ---------------------------------------------------------------------------


def test_sigma_converges_as_the_tree_refines() -> None:
    K, T, sigma = 100.0, 0.5, 0.30
    coarse = implied_vol_american(_amer(K, T, sigma, "put", n_steps=401),
                                  S0, K, T, R, "put", q=0.0, n_steps=401)
    fine = implied_vol_american(_amer(K, T, sigma, "put", n_steps=801),
                                S0, K, T, R, "put", q=0.0, n_steps=801)
    assert coarse == pytest.approx(sigma, abs=1e-5)
    assert fine == pytest.approx(sigma, abs=1e-5)


def test_n_steps_must_match_the_tree_that_made_the_price() -> None:
    """Why from_market passes the engine's own step count.

    Invert an 801-step price on a 401-step tree and sigma moves by far more
    than the solver's tolerance -- the discretisation error, not solver error.
    """
    K, T, sigma = 100.0, 0.5, 0.30
    px = _amer(K, T, sigma, "put", n_steps=801)
    matched = implied_vol_american(px, S0, K, T, R, "put", q=0.0, n_steps=801)
    mismatched = implied_vol_american(px, S0, K, T, R, "put", q=0.0, n_steps=401)
    assert matched == pytest.approx(sigma, abs=1e-5)
    assert abs(mismatched - sigma) > 1e-5


def test_vol_floor_keeps_the_tree_arbitrage_free() -> None:
    """Below |r-q|*sqrt(dt) the CRR tree raises; the search must start above it."""
    T, n = 0.25, 51
    floor = crr_vol_floor(T, R, 0.0, n)
    assert floor > abs(R - 0.0) * math.sqrt(T / n)
    # The floor is priceable; meaningfully under it, the tree refuses.
    assert _amer(100.0, T, floor, "put", n_steps=n) >= 0.0
    with pytest.raises(ValueError, match="not arbitrage-free"):
        crr_price(S0, 100.0, T, R, floor / 100.0, "put", q=0.0, n_steps=n, american=True)


# ---------------------------------------------------------------------------
# Bounds and the zero boundary
# ---------------------------------------------------------------------------


def test_american_bounds_are_not_the_european_ones() -> None:
    """Undiscounted intrinsic below, and K (not Ke^-rT) above, for a put."""
    K, T = 120.0, 1.0
    lower, upper = american_bounds(S0, K, T, R, "put", q=0.0)
    assert lower == pytest.approx(K - S0)          # exercise now
    assert upper == pytest.approx(K)               # undiscounted
    assert upper > K * math.exp(-R * T)            # strictly above the European cap


def test_price_at_intrinsic_returns_zero_not_an_error() -> None:
    """A deep ITM American put is exercised now, so no sigma reproduces it."""
    K, T = 130.0, 0.25
    px = _amer(K, T, 0.20, "put")
    assert px == pytest.approx(K - S0)
    assert implied_vol_american(px, S0, K, T, R, "put", q=0.0, n_steps=STEPS) == 0.0


@pytest.mark.parametrize(
    "price,call_put,match",
    [
        (130.0, "put", "above max bound"),      # a put cannot exceed K=120
        (101.0, "call", "above max bound"),     # a call cannot exceed S
        (-1.0, "put", "is negative"),
        (float("nan"), "put", "non-finite"),
    ],
)
def test_fails_loud_never_nan(price: float, call_put: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        implied_vol_american(price, S0, 120.0, 1.0, R, call_put, q=0.0, n_steps=51)


def test_rejects_a_price_below_intrinsic() -> None:
    K, T = 130.0, 0.25
    with pytest.raises(ValueError, match="below intrinsic bound"):
        implied_vol_american(K - S0 - 1.0, S0, K, T, R, "put", q=0.0, n_steps=STEPS)


@pytest.mark.parametrize("bad", [0, 1])
def test_rejects_a_degenerate_tree(bad: int) -> None:
    with pytest.raises(ValueError, match="n_steps must be >= 2"):
        implied_vol_american(5.0, S0, 100.0, 0.5, R, "put", q=0.0, n_steps=bad)
