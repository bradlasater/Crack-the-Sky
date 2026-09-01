"""IV inversion: round-trip, fail-loud, never NaN."""

from __future__ import annotations

import math

import pytest

from pricing.bsm import price
from pricing.iv import implied_vol


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
    # Deep ITM call intrinsic ~ 50 * e^{-qT} - 50 * e^{-rT} wait S=100 K=50
    with pytest.raises(ValueError, match="below intrinsic"):
        implied_vol(0.01, 100.0, 50.0, 1.0, 0.05, "call", q=0.0)


def test_price_above_spot_bound() -> None:
    with pytest.raises(ValueError, match="above max"):
        implied_vol(200.0, 100.0, 100.0, 1.0, 0.05, "call", q=0.0)


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
