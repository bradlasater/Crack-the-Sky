"""American CRR bump-and-revalue vs European in the zero-early-exercise limit.

Deep OTM short-dated puts are excepted: the tree is noisy near zero value.
"""

from __future__ import annotations

import pytest

from pricing.bsm import price as bsm_price
from pricing.conventions import GREEK_NAMES, GreeksConventions
from pricing.engine import AmericanCRR, EuropeanBSM, crr_price

CONV = GreeksConventions(
    vega_unit="per_1.00",
    theta_unit="per_year",
    delta_kind="spot",
    gamma_kind="spot",
)
EURO = EuropeanBSM()
AMER = AmericanCRR(n_steps=401)


def test_american_call_q0_matches_european() -> None:
    """American call with q=0 is never exercised early."""
    S, K, T, r, sig = 100.0, 100.0, 0.5, 0.05, 0.20
    euro = bsm_price(S, K, T, r, sig, "call", q=0.0)
    tree_am = crr_price(S, K, T, r, sig, "call", q=0.0, n_steps=801, american=True)
    tree_eu = crr_price(S, K, T, r, sig, "call", q=0.0, n_steps=801, american=False)
    assert tree_am == pytest.approx(tree_eu, rel=1e-10, abs=1e-12)
    assert tree_am == pytest.approx(euro, rel=2e-3, abs=1e-3)


def test_american_put_has_early_exercise_premium() -> None:
    S, K, T, r, sig = 100.0, 100.0, 1.0, 0.08, 0.20
    euro = bsm_price(S, K, T, r, sig, "put", q=0.0)
    amer = crr_price(S, K, T, r, sig, "put", q=0.0, n_steps=401, american=True)
    assert amer > euro + 0.01


def test_engine_greeks_names() -> None:
    cat = AMER.greeks(100, 100, 0.5, 0.05, 0.2, "call", q=0.0, conventions=CONV)
    for name in GREEK_NAMES:
        val = getattr(cat, name)
        assert val == val  # not NaN


def test_american_call_q0_greeks_near_european() -> None:
    S, K, T, r, sig = 100.0, 100.0, 0.5, 0.05, 0.20
    am = AMER.greeks(S, K, T, r, sig, "call", q=0.0, conventions=CONV)
    eu = EURO.greeks(S, K, T, r, sig, "call", q=0.0, conventions=CONV)
    assert am.price == pytest.approx(eu.price, rel=5e-3)
    assert am.delta == pytest.approx(eu.delta, rel=5e-2, abs=1e-3)
    assert am.vega == pytest.approx(eu.vega, rel=8e-2, abs=1e-2)


def test_deep_otm_short_put_excepted_from_tight_match() -> None:
    """Document the exception: do not require American ≈ European here."""
    S, K, T, r, sig = 100.0, 50.0, 7 / 365, 0.05, 0.20
    euro = float(bsm_price(S, K, T, r, sig, "put", q=0.0))
    amer = crr_price(S, K, T, r, sig, "put", q=0.0, n_steps=201, american=True)
    assert euro >= 0.0 and amer >= 0.0
    assert euro < 0.05 and amer < 0.05
