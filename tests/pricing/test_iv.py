"""IV inversion: round-trip, fail-loud, never NaN."""

from __future__ import annotations

import math

import pytest

from pricing.bsm import price
from pricing.iv import BelowIntrinsicError, implied_vol


def test_roundtrip_atm() -> None:
    S, K, T, r, q, sig = 100.0, 100.0, 0.5, 0.05, 0.01, 0.23
    px = price(S, K, T, r, sig, "call", q=q)
    iv = implied_vol(px, S, K, T, r, "call", q=q)
    assert iv == pytest.approx(sig, rel=1e-8, abs=1e-10)
    assert math.isfinite(iv)


def test_roundtrip_put_otm() -> None:
    S, K, T, r, q, sig = 100.0, 90.0, 1.0, 0.03, 0.0, 0.40
    px = price(S, K, T, r, sig, "put", q=q)
    iv = implied_vol(px, S, K, T, r, "put", q=q)
    assert iv == pytest.approx(sig, rel=1e-6, abs=1e-8)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"market_price": 1.0, "S": 0.0, "K": 100.0, "T": 1.0, "r": 0.0},
        {"market_price": 1.0, "S": 100.0, "K": -1.0, "T": 1.0, "r": 0.0},
        {"market_price": 1.0, "S": 100.0, "K": 100.0, "T": 0.0, "r": 0.0},
        {"market_price": 1.0, "S": 100.0, "K": 100.0, "T": -0.1, "r": 0.0},
    ],
)
def test_invalid_inputs_value_error(kwargs) -> None:
    with pytest.raises(ValueError):
        implied_vol(**kwargs, call_put="call", q=0.0)


def test_price_below_intrinsic() -> None:
    S, K, T, r, q = 100.0, 50.0, 1.0, 0.05, 0.03
    lower = max(S * math.exp(-q * T) - K * math.exp(-r * T), 0.0)
    with pytest.raises(BelowIntrinsicError, match="below intrinsic"):
        implied_vol(lower - 1.0, S, K, T, r, "call", q=q)


def test_price_above_discounted_spot_bound() -> None:
    """European call cap is Se^{-qT}, not S. q>0 so the two differ."""
    S, K, T, r, q = 100.0, 100.0, 1.0, 0.05, 0.03
    upper = S * math.exp(-q * T)
    assert upper < S
    with pytest.raises(ValueError, match="above max"):
        implied_vol(0.5 * (upper + S), S, K, T, r, "call", q=q)


def test_european_put_rejects_price_above_discounted_strike() -> None:
    """European put cap is Ke^{-rT}, not K."""
    S, K, T, r, q = 100.0, 100.0, 1.0, 0.05, 0.0
    upper = K * math.exp(-r * T)
    assert upper < K
    with pytest.raises(ValueError, match="above max"):
        implied_vol(0.5 * (upper + K), S, K, T, r, "put", q=q)


def test_price_at_discounted_intrinsic_returns_zero() -> None:
    S, K, T, r, q = 100.0, 50.0, 1.0, 0.05, 0.03
    lower = max(S * math.exp(-q * T) - K * math.exp(-r * T), 0.0)
    assert lower < S - K  # discounted floor, not S-K
    assert implied_vol(lower, S, K, T, r, "call", q=q) == 0.0


def test_put_price_at_discounted_intrinsic_returns_zero() -> None:
    S, K, T, r, q = 50.0, 100.0, 1.0, 0.05, 0.0
    lower = max(K * math.exp(-r * T) - S * math.exp(-q * T), 0.0)
    assert lower < K - S  # Ke^{-rT}-S, not K-S
    assert implied_vol(lower, S, K, T, r, "put", q=q) == 0.0


def test_never_returns_nan() -> None:
    cases = [
        (float("nan"), 100.0, 100.0, 1.0, 0.05),
        (1.0, float("nan"), 100.0, 1.0, 0.05),
        (-1.0, 100.0, 100.0, 1.0, 0.05),
    ]
    for args in cases:
        with pytest.raises(ValueError):
            out = implied_vol(*args, call_put="call", q=0.0)
            assert out == out  # noqa: B011 - would fire if NaN leaked


def test_roundtrip_via_forward_matches_q() -> None:
    S, K, T, r, q, sig = 100.0, 100.0, 0.5, 0.05, 0.02, 0.25
    F = S * math.exp((r - q) * T)
    px = price(S, K, T, r, sig, "call", F=F)
    assert implied_vol(px, S, K, T, r, "call", F=F) == pytest.approx(sig, rel=1e-8, abs=1e-10)
    assert implied_vol(px, S, K, T, r, "call", q=q) == pytest.approx(sig, rel=1e-8, abs=1e-10)


def test_newton_recovers_sigma_without_brent(monkeypatch: pytest.MonkeyPatch) -> None:
    """σ ← σ - (model - target) / vega, vega per 1.00. Brent must not be required."""

    def boom(*_a, **_k):
        raise AssertionError("Newton should have converged before Brent")

    monkeypatch.setattr("pricing.iv.brentq", boom)
    S, K, T, r, q, sig = 100.0, 100.0, 0.5, 0.05, 0.01, 0.23
    px = price(S, K, T, r, sig, "call", q=q)
    iv = implied_vol(px, S, K, T, r, "call", q=q)
    assert iv == pytest.approx(sig, rel=1e-8, abs=1e-10)


def test_newton_recovers_put_sigma_without_brent(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("Newton should have converged before Brent")

    monkeypatch.setattr("pricing.iv.brentq", boom)
    S, K, T, r, q, sig = 100.0, 90.0, 1.0, 0.03, 0.0, 0.40
    px = price(S, K, T, r, sig, "put", q=q)
    iv = implied_vol(px, S, K, T, r, "put", q=q)
    assert iv == pytest.approx(sig, rel=1e-6, abs=1e-8)
