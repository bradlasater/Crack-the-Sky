"""Independent recomputation of every bound, seed, and CRR-floor formula.

Each assertion is written so a wrong coefficient, sign, or exponent fails:
call upper S instead of Se^{-qT}, put upper Ke^{-rT} instead of K (American),
seed √(2π T) instead of √(2π/T), floor 1.0×|r-q|√dt instead of 1.5×.
"""

from __future__ import annotations

import math

import pytest

from pricing.iv import (
    american_bounds,
    brenner_subrahmanyam_seed,
    crr_vol_floor,
    discounted_bounds,
)

# Known point used for the European/American numeric pins. q and r both
# nonzero and unequal so Se^{-qT}, Se^{-rT}, S and F e^{-rT} all split.
S, K, T, R, Q = 100.0, 90.0, 0.75, 0.04, 0.02


def _disc_s(s: float = S, q: float = Q, t: float = T) -> float:
    return s * math.exp(-q * t)


def _disc_k(k: float = K, r: float = R, t: float = T) -> float:
    return k * math.exp(-r * t)


# ---------------------------------------------------------------------------
# European discounted_bounds
# ---------------------------------------------------------------------------


def test_european_call_bounds_exact() -> None:
    lower, upper = discounted_bounds(S, K, T, R, "call", q=Q)
    assert lower == pytest.approx(max(_disc_s() - _disc_k(), 0.0), abs=1e-12)
    assert upper == pytest.approx(_disc_s(), abs=1e-12)
    assert upper == pytest.approx(98.51119396030626, abs=1e-12)
    assert lower == pytest.approx(11.171095940940518, abs=1e-12)


def test_european_put_bounds_exact() -> None:
    lower, upper = discounted_bounds(S, K, T, R, "put", q=Q)
    assert lower == pytest.approx(max(_disc_k() - _disc_s(), 0.0), abs=1e-12)
    assert upper == pytest.approx(_disc_k(), abs=1e-12)
    assert upper == pytest.approx(87.34009801936574, abs=1e-12)
    assert lower == 0.0


def test_european_call_upper_is_discounted_spot_not_s() -> None:
    _, upper = discounted_bounds(S, K, T, R, "call", q=Q)
    assert upper == pytest.approx(_disc_s(), abs=1e-12)
    assert abs(upper - S) > 1.0


def test_european_put_upper_is_discounted_strike_not_k() -> None:
    _, upper = discounted_bounds(S, K, T, R, "put", q=Q)
    assert upper == pytest.approx(_disc_k(), abs=1e-12)
    assert abs(upper - K) > 1.0


def test_european_otm_call_lower_is_zero() -> None:
    lower, _ = discounted_bounds(100.0, 200.0, 1.0, 0.05, "call", q=0.03)
    euro = 100.0 * math.exp(-0.03) - 200.0 * math.exp(-0.05)
    assert euro < 0.0
    assert lower == 0.0


def test_european_itm_put_lower_is_discounted_intrinsic() -> None:
    s, k, t, r, q = 50.0, 100.0, 1.0, 0.05, 0.0
    lower, _ = discounted_bounds(s, k, t, r, "put", q=q)
    expected = k * math.exp(-r * t) - s * math.exp(-q * t)
    assert lower == pytest.approx(expected, abs=1e-12)
    assert abs(lower - (k - s)) > 1.0  # not K-S


def test_discounted_bounds_q_and_f_agree() -> None:
    F = S * math.exp((R - Q) * T)
    assert F != S
    q_bounds = discounted_bounds(S, K, T, R, "call", q=Q)
    f_bounds = discounted_bounds(S, K, T, R, "call", F=F)
    assert f_bounds[0] == pytest.approx(q_bounds[0], abs=1e-12)
    assert f_bounds[1] == pytest.approx(q_bounds[1], abs=1e-12)
    # Black-76 form: Se^{-qT} = F e^{-rT}
    assert f_bounds[1] == pytest.approx(F * math.exp(-R * T), abs=1e-12)


def test_discounted_bounds_f_path_is_not_q_zero() -> None:
    """If F is ignored, q defaults to 0 and the call cap becomes S."""
    F = S * math.exp((R - Q) * T)
    _, upper = discounted_bounds(S, K, T, R, "call", F=F)
    assert abs(upper - S) > 1.0
    assert upper == pytest.approx(_disc_s(), abs=1e-12)


def test_discounted_bounds_put_f_path() -> None:
    F = S * math.exp((R - Q) * T)
    q_bounds = discounted_bounds(S, K, T, R, "put", q=Q)
    f_bounds = discounted_bounds(S, K, T, R, "put", F=F)
    assert f_bounds[0] == pytest.approx(q_bounds[0], abs=1e-12)
    assert f_bounds[1] == pytest.approx(q_bounds[1], abs=1e-12)


# ---------------------------------------------------------------------------
# American bounds
# ---------------------------------------------------------------------------


def test_american_call_bounds_exact() -> None:
    lower, upper = american_bounds(S, K, T, R, "call", q=Q)
    euro = _disc_s() - _disc_k()
    assert lower == pytest.approx(max(euro, S - K, 0.0), abs=1e-12)
    assert upper == pytest.approx(S)
    # This point: euro floor 11.17 beats S-K=10, upper is undiscounted S.
    assert lower == pytest.approx(euro, abs=1e-12)
    assert lower > S - K
    assert upper == S
    assert abs(upper - _disc_s()) > 1.0


def test_american_put_bounds_exact() -> None:
    k, t = 120.0, 1.0
    lower, upper = american_bounds(S, k, t, R, "put", q=0.0)
    euro = k * math.exp(-R * t) - S
    assert lower == pytest.approx(max(euro, k - S, 0.0), abs=1e-12)
    assert upper == pytest.approx(k)
    assert lower == pytest.approx(k - S)
    assert abs(upper - k * math.exp(-R * t)) > 1.0


def test_american_call_lower_uses_undiscounted_intrinsic_when_q_is_high() -> None:
    s, k, t, r, q = 100.0, 80.0, 1.0, 0.01, 0.20
    lower, upper = american_bounds(s, k, t, r, "call", q=q)
    euro = s * math.exp(-q * t) - k * math.exp(-r * t)
    assert euro < s - k
    assert lower == pytest.approx(s - k)
    assert upper == s


def test_american_put_lower_uses_european_floor_when_it_dominates() -> None:
    s, k, t, r, q = 100.0, 120.0, 1.0, 0.0, 0.05
    lower, _ = american_bounds(s, k, t, r, "put", q=q)
    euro = k * math.exp(-r * t) - s * math.exp(-q * t)
    assert euro > k - s
    assert lower == pytest.approx(euro, abs=1e-12)


def test_american_call_upper_is_s_not_discounted_spot() -> None:
    _, upper = american_bounds(S, K, T, R, "call", q=Q)
    assert upper == S
    assert upper != pytest.approx(_disc_s())


def test_american_put_upper_is_k_not_discounted_strike() -> None:
    _, upper = american_bounds(S, K, T, R, "put", q=Q)
    assert upper == K
    assert upper != pytest.approx(_disc_k())


def test_american_bounds_q_and_f_agree() -> None:
    F = S * math.exp((R - Q) * T)
    for cp in ("call", "put"):
        q_bounds = american_bounds(S, K, T, R, cp, q=Q)
        f_bounds = american_bounds(S, K, T, R, cp, F=F)
        assert f_bounds[0] == pytest.approx(q_bounds[0], abs=1e-12)
        assert f_bounds[1] == pytest.approx(q_bounds[1], abs=1e-12)


# ---------------------------------------------------------------------------
# CRR vol floor: σ_min = max(1.5 |r-q| √(T/n), 1e-6)
# ---------------------------------------------------------------------------


def test_crr_vol_floor_is_one_point_five_times_drift_boundary() -> None:
    t, r, q, n = 0.25, 0.01, 0.05, 51
    dt = t / n
    expected = 1.5 * abs(r - q) * math.sqrt(dt)
    got = crr_vol_floor(t, r, q, n)
    assert got == pytest.approx(expected, abs=1e-16)
    # Pins abs(): r-q is negative here. Without abs, max(negative, 1e-6) = 1e-6.
    assert expected > 1e-3
    assert got != pytest.approx(1.0 * abs(r - q) * math.sqrt(dt))
    assert got != pytest.approx(1.5 * abs(r - q) * math.sqrt(t) / n)


def test_crr_vol_floor_hits_absolute_minimum_when_rates_match() -> None:
    assert crr_vol_floor(0.25, 0.05, 0.05, 51) == 1e-6
    # Tiny carry still under the 1e-6 floor.
    assert crr_vol_floor(0.25, 0.05, 0.05 + 1e-12, 51) == 1e-6


# ---------------------------------------------------------------------------
# Brenner–Subrahmanyam seed: σ ≈ √(2π/T) * price / (S e^{-qT})
# ---------------------------------------------------------------------------


def test_brenner_subrahmanyam_seed_is_sqrt_two_pi_over_t() -> None:
    s, t, q, px = 100.0, 0.25, 0.02, 4.0
    disc_s = s * math.exp(-q * t)
    got = brenner_subrahmanyam_seed(px, s, t, q)
    expected = math.sqrt(2.0 * math.pi / t) * (px / disc_s)
    wrong_time = math.sqrt(2.0 * math.pi * t) * (px / disc_s)
    wrong_spot = math.sqrt(2.0 * math.pi / t) * (px / s)
    assert got == pytest.approx(expected, abs=1e-15)
    assert abs(got - wrong_time) > 0.1
    assert abs(got - wrong_spot) > 1e-4
    # ATM identity: price = Se^{-qT} σ √(T/2π) inverts to σ exactly.
    sig = 0.20
    atm_px = disc_s * sig * math.sqrt(t / (2.0 * math.pi))
    assert brenner_subrahmanyam_seed(atm_px, s, t, q) == pytest.approx(sig, abs=1e-15)
