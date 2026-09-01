"""Systematic finite-difference audit of every Greek, for BOTH calls and puts.

A put-only sign error in `charm` shipped because the existing finite-difference
test exercised calls only: `dDelta_p_dT` had a minus where the two sign flips
(the leading minus on the put delta, and d/dT N(-d1) = -n(d1) d(d1)/dT) cancel
to a plus. It reported +0.0894 where the true value is -0.1199, and it violated
put-call parity.

This audit is parameterised over both option types and several regimes so the
same class of error cannot recur silently.
"""

from __future__ import annotations

import numpy as np
import pytest

from pricing.bsm import price, raw_greeks

# (S, K, r, q, sigma, T): ATM-ish, SPX-like, deep ITM w/ dividend, short-dated
CASES = [
    (100.0, 105.0, 0.05, 0.03, 0.25, 0.75),
    (7691.0, 7700.0, 0.04, 0.015, 0.15, 0.12),
    (767.0, 700.0, 0.045, 0.012, 0.30, 2.00),
    (50.0, 50.0, 0.00, 0.00, 0.60, 0.05),
]
CALL_PUT = ["call", "put"]


def _v(S, K, T, r, sigma, q, cp):
    return float(price(S, K, T, r, sigma, cp, q=q))


def _g(name, S, K, T, r, sigma, q, cp):
    return float(raw_greeks(S, K, T, r, sigma, cp, q=q)[name])


def _d1(f, x, h):
    return (f(x + h) - f(x - h)) / (2.0 * h)


def _d2(f, x, h):
    return (f(x + h) - 2.0 * f(x) + f(x - h)) / (h * h)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("cp", CALL_PUT)
@pytest.mark.parametrize(
    "greek",
    ["delta", "gamma", "vega", "theta", "rho", "rho_dividend", "dual_delta",
     "vanna", "volga", "charm", "veta", "vera", "speed", "zomma", "color"],
)
def test_greek_matches_finite_difference(greek: str, cp: str, case) -> None:
    S, K, r, q, sigma, T = case
    hS, hK, hs, hT, hr = max(1e-4, S * 1e-6), max(1e-4, K * 1e-6), 1e-6, 1e-6, 1e-7

    V = lambda **kw: _v(kw.get("S", S), kw.get("K", K), kw.get("T", T),  # noqa: E731
                        kw.get("r", r), kw.get("sigma", sigma), kw.get("q", q), cp)
    G = lambda n, **kw: _g(n, kw.get("S", S), kw.get("K", K), kw.get("T", T),  # noqa: E731
                           kw.get("r", r), kw.get("sigma", sigma), kw.get("q", q), cp)

    fd = {
        # First order
        "delta": lambda: _d1(lambda x: V(S=x), S, hS),
        "vega": lambda: _d1(lambda x: V(sigma=x), sigma, hs),
        "rho": lambda: _d1(lambda x: V(r=x), r, hr),
        "rho_dividend": lambda: _d1(lambda x: V(q=x), q, hr),
        "dual_delta": lambda: _d1(lambda x: V(K=x), K, hK),
        "theta": lambda: -_d1(lambda x: V(T=x), T, hT),      # calendar time
        # Second order
        "gamma": lambda: _d2(lambda x: V(S=x), S, S * 1e-4),
        "vanna": lambda: _d1(lambda x: G("vega", S=x), S, hS),
        "volga": lambda: _d1(lambda x: G("vega", sigma=x), sigma, hs),
        "charm": lambda: -_d1(lambda x: G("delta", T=x), T, hT),
        "veta": lambda: -_d1(lambda x: G("vega", T=x), T, hT),
        "vera": lambda: _d1(lambda x: G("rho", sigma=x), sigma, hs),
        # Third order
        "speed": lambda: _d1(lambda x: G("gamma", S=x), S, S * 1e-4),
        "zomma": lambda: _d1(lambda x: G("gamma", sigma=x), sigma, hs),
        "color": lambda: -_d1(lambda x: G("gamma", T=x), T, hT),
    }[greek]()

    closed = G(greek)
    assert closed == pytest.approx(fd, rel=2e-4, abs=2e-4), (
        f"{greek} {cp}: closed-form {closed:+.9f} vs finite-diff {fd:+.9f}"
    )


# ---------------------------------------------------------------------------
# Parity identities: cheap, exact, and independent of finite differences
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES)
def test_put_call_parity_on_price(case) -> None:
    S, K, r, q, sigma, T = case
    c = _v(S, K, T, r, sigma, q, "call")
    p = _v(S, K, T, r, sigma, q, "put")
    assert c - p == pytest.approx(
        S * np.exp(-q * T) - K * np.exp(-r * T), rel=1e-12, abs=1e-9
    )


@pytest.mark.parametrize("case", CASES)
def test_put_call_parity_on_delta(case) -> None:
    S, K, r, q, sigma, T = case
    dc = _g("delta", S, K, T, r, sigma, q, "call")
    dp = _g("delta", S, K, T, r, sigma, q, "put")
    assert dc - dp == pytest.approx(np.exp(-q * T), rel=1e-12)


@pytest.mark.parametrize("case", CASES)
def test_put_call_parity_on_charm(case) -> None:
    """The identity that pins the sign the put branch had wrong.

    delta_c - delta_p = e^{-qT}  =>  d/dT(delta_c - delta_p) = -q e^{-qT},
    and charm = -d(delta)/dT, so charm_c - charm_p = +q e^{-qT}.
    """
    S, K, r, q, sigma, T = case
    cc = _g("charm", S, K, T, r, sigma, q, "call")
    cp_ = _g("charm", S, K, T, r, sigma, q, "put")
    assert cc - cp_ == pytest.approx(q * np.exp(-q * T), rel=1e-9, abs=1e-12)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("greek", ["gamma", "vega", "volga", "vanna", "veta",
                                   "zomma", "color", "speed", "ultima"])
def test_greeks_identical_for_calls_and_puts(greek: str, case) -> None:
    """Second-order-in-S/sigma Greeks do not depend on the option type."""
    S, K, r, q, sigma, T = case
    assert _g(greek, S, K, T, r, sigma, q, "call") == pytest.approx(
        _g(greek, S, K, T, r, sigma, q, "put"), rel=1e-12
    )


def test_charm_put_regression_value() -> None:
    """Pins the specific number the bug got wrong."""
    got = _g("charm", 100.0, 105.0, 0.75, 0.05, 0.25, 0.03, "put")
    assert got == pytest.approx(-0.119874416, abs=1e-7)
    assert got < 0, "the bug reported +0.0894 here"
