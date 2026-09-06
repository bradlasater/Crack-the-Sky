"""First-principles pins for BSM ingredients that other tests only hit indirectly.

Haug tables and finite-difference audits already lock price and the named
Greeks. This file pins d1/d2, dual-gamma, and the F→q inversion against the
textbook Merton (1973) expressions, so a wrong sign, coefficient, or exponent
fails even if a downstream Greek still looks plausible.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.stats import norm

from pricing.bsm import _core, greeks, resolve_q
from pricing.conventions import GreeksConventions

SPOT_YEAR = GreeksConventions(
    vega_unit="per_1.00",
    theta_unit="per_year",
    delta_kind="spot",
    gamma_kind="spot",
)

# OTM-ish: the σ²/2 term, ln(S/K), and (r-q)T are all comparable.
S, K, T, R, Q, SIG = 100.0, 105.0, 0.75, 0.06, 0.03, 0.25


def test_d1_d2_merton_formula() -> None:
    sqrtT = np.sqrt(T)
    d1 = (np.log(S / K) + (R - Q + 0.5 * SIG**2) * T) / (SIG * sqrtT)
    d2 = (np.log(S / K) + (R - Q - 0.5 * SIG**2) * T) / (SIG * sqrtT)
    got = _core(S, K, T, R, SIG, q=Q)
    assert float(got["d1"]) == pytest.approx(d1, rel=1e-15, abs=1e-15)
    assert float(got["d2"]) == pytest.approx(d2, rel=1e-15, abs=1e-15)
    assert float(got["d2"]) == pytest.approx(d1 - SIG * sqrtT, rel=1e-15, abs=1e-15)


def test_d1_d2_atm_half_variance_when_r_equals_q() -> None:
    """ATM, r = q ⇒ d1 = +σ√T/2, d2 = −σ√T/2. Pins the 1/2 in the σ² term."""
    s, t, r, sig = 100.0, 1.0, 0.05, 0.20
    half = 0.5 * sig * np.sqrt(t)
    got = _core(s, s, t, r, sig, q=r)
    assert float(got["d1"]) == pytest.approx(half, rel=1e-15, abs=1e-15)
    assert float(got["d2"]) == pytest.approx(-half, rel=1e-15, abs=1e-15)


def test_d1_d2_from_forward() -> None:
    """F = S e^{(r-q)T} ⇒ d1 = [ln(F/K) + σ²T/2] / (σ√T)."""
    F = S * np.exp((R - Q) * T)
    sqrtT = np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * SIG**2 * T) / (SIG * sqrtT)
    d2 = (np.log(F / K) - 0.5 * SIG**2 * T) / (SIG * sqrtT)
    got = _core(S, K, T, R, SIG, F=F)
    assert float(got["d1"]) == pytest.approx(d1, rel=1e-15, abs=1e-15)
    assert float(got["d2"]) == pytest.approx(d2, rel=1e-15, abs=1e-15)


@pytest.mark.parametrize("cp", ["call", "put"])
def test_dual_gamma_closed_form(cp: str) -> None:
    d2 = (np.log(S / K) + (R - Q - 0.5 * SIG**2) * T) / (SIG * np.sqrt(T))
    expected = np.exp(-R * T) * norm.pdf(d2) / (K * SIG * np.sqrt(T))
    cat = greeks(S, K, T, R, SIG, cp, q=Q, conventions=SPOT_YEAR)
    assert cat.dual_gamma == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize(
    "s, t, r, q",
    [
        (100.0, 0.75, 0.06, 0.03),   # F > S
        (100.0, 0.50, 0.01, 0.04),   # F < S, pins the sign of ln(F/S)
        (767.0, 2.00, 0.045, 0.012),
    ],
)
def test_resolve_q_inverts_forward(s: float, t: float, r: float, q: float) -> None:
    F = s * np.exp((r - q) * t)
    q_got = float(resolve_q(s, t, r, F=F))
    assert q_got == pytest.approx(r - np.log(F / s) / t, rel=1e-15, abs=1e-15)
    assert s * np.exp((r - q_got) * t) == pytest.approx(F, rel=1e-15, abs=1e-15)
    assert q_got == pytest.approx(q, rel=1e-15, abs=1e-15)
