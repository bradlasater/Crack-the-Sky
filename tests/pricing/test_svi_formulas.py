"""Pin raw-SVI formulas against Gatheral–Jacquier (Quantitative Finance 14(1), 2014).

Production code is compared to independently written closed forms and to
hand-derived exact values (nice parameters where 1-ρ² = 16/25). A wrong
coefficient, sign, or exponent in ``pricing.surface`` must fail a test here:
g(k) with 1/4 in place of 1/w+1/4, w' with d/sq flipped, interpolation in
σ instead of total variance, and so on.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest

from pricing import surface as sf
from pricing.bsm import price

# 1 - ρ² = 0.64, √(1-ρ²) = 0.8. Several derivatives collapse to rationals in √2.
A, B, RHO, M, SIG = 0.04, 0.4, -0.6, 0.1, 0.2

# Gatheral–Jacquier Example 3.1 (Axel Vogt): g(k) goes negative.
VOGT = {"a": -0.0410, "b": 0.1331, "rho": 0.3060, "m": 0.3586, "sigma": 0.4153}

DAY = date(2026, 8, 28)


def _gatheral_w(k: float, a: float, b: float, rho: float, m: float, sigma: float) -> float:
    """Raw SVI, Gatheral–Jacquier eq. (3.1)."""
    return a + b * (rho * (k - m) + math.sqrt((k - m) ** 2 + sigma**2))


def _gatheral_wp_wpp(
    k: float,
    b: float,
    rho: float,
    m: float,
    sigma: float,
) -> tuple[float, float]:
    """∂w/∂k and ∂²w/∂k² from differentiating eq. (3.1)."""
    disc = (k - m) ** 2 + sigma**2
    root = math.sqrt(disc)
    wp = b * (rho + (k - m) / root)
    wpp = b * sigma**2 * disc**-1.5
    return wp, wpp


def _gatheral_g(k: float, a: float, b: float, rho: float, m: float, sigma: float) -> float:
    """Butterfly density condition, Gatheral–Jacquier eq. (2.1)."""
    w = _gatheral_w(k, a, b, rho, m, sigma)
    wp, wpp = _gatheral_wp_wpp(k, b, rho, m, sigma)
    return (1.0 - k * wp / (2.0 * w)) ** 2 - (wp**2 / 4.0) * (1.0 / w + 1.0 / 4.0) + wpp / 2.0


def _prod_g(
    k: float, a: float = A, b: float = B, rho: float = RHO, m: float = M, sigma: float = SIG
) -> float:
    return float(sf._g(np.array([k], dtype=float), a, b, rho, m, sigma)[0])


def _slice(
    expiry: str,
    t: float,
    F: float,
    a: float,
    b: float = 0.0,
    rho: float = 0.0,
    m: float = 0.0,
    sigma: float = 0.1,
) -> sf.Slice:
    return sf.Slice(
        expiration_date=expiry,
        dte=max(int(round(t * 365.0)), 1),
        t_years=t,
        forward=F,
        a=a,
        b=b,
        rho=rho,
        m=m,
        sigma=sigma,
        k_min=-0.5,
        k_max=0.5,
        n_strikes=11,
        rms_error=0.0,
        min_g=1.0,
        rate=0.04,
    )


# ---------------------------------------------------------------------------
# Raw SVI w(k) = a + b {ρ(k-m) + √((k-m)² + σ²)}
# ---------------------------------------------------------------------------


def test_raw_svi_w_at_k_equals_m_is_a_plus_b_sigma() -> None:
    """At k = m the square root collapses to σ, so w = a + b σ."""
    assert sf._svi_w(M, A, B, RHO, M, SIG) == pytest.approx(A + B * SIG)
    assert sf._svi_w(M, A, B, RHO, M, SIG) == pytest.approx(0.12)


def test_raw_svi_w_matches_hand_value_involving_sqrt2() -> None:
    """k = m+σ: √(d²+σ²) = σ√2, w = a + b(ρσ + σ√2) = 0.08√2 - 0.008."""
    k = M + SIG  # 0.3
    expected = 0.08 * math.sqrt(2.0) - 0.008
    assert float(sf._svi_w(k, A, B, RHO, M, SIG)) == pytest.approx(expected, rel=1e-15)
    assert _gatheral_w(k, A, B, RHO, M, SIG) == pytest.approx(expected, rel=1e-15)


def test_raw_svi_w_sign_on_rho_term() -> None:
    """Flipping ρ(k-m) to -ρ(k-m) moves w by 2 b ρ d, here -0.096."""
    k = M + SIG
    d = k - M
    got = float(sf._svi_w(k, A, B, RHO, M, SIG))
    flipped = A + B * (-RHO * d + math.sqrt(d * d + SIG * SIG))
    assert flipped != pytest.approx(got, abs=1e-6)
    assert flipped - got == pytest.approx(-2.0 * B * RHO * d, rel=1e-12)


def test_slice_total_variance_matches_svi_w() -> None:
    sl = _slice("2026-09-25", 0.25, 100.0, A, B, RHO, M, SIG)
    for k in (-0.4, 0.0, M, 0.3, 0.5):
        assert sl.total_variance(k) == pytest.approx(
            float(sf._svi_w(k, A, B, RHO, M, SIG)), rel=1e-15
        )


# ---------------------------------------------------------------------------
# Domain: min_k w = a + b σ √(1-ρ²) at k* = m - ρ σ / √(1-ρ²)
# ---------------------------------------------------------------------------


def test_min_variance_location_and_value() -> None:
    s = math.sqrt(1.0 - RHO * RHO)
    assert s == pytest.approx(0.8)
    k_star = M - RHO * SIG / s
    w_min = A + B * SIG * s
    assert k_star == pytest.approx(0.25)
    assert w_min == pytest.approx(0.104)
    assert float(sf._svi_w(k_star, A, B, RHO, M, SIG)) == pytest.approx(w_min, rel=1e-15)
    wp, wpp = _gatheral_wp_wpp(k_star, B, RHO, M, SIG)
    assert wp == pytest.approx(0.0, abs=1e-15)
    assert wpp > 0.0
    # A dense grid cannot undercut the closed-form minimum.
    grid = np.linspace(k_star - 1.0, k_star + 1.0, 4001)
    assert float(np.min(sf._svi_w(grid, A, B, RHO, M, SIG))) >= w_min - 1e-12


def test_min_variance_k_star_moves_opposite_rho() -> None:
    s = math.sqrt(1.0 - 0.6**2)
    assert (M - (0.6) * SIG / s) < M
    assert (M - (-0.6) * SIG / s) > M
    assert (M - 0.0 * SIG / 1.0) == M


def test_a_from_w0_inversion() -> None:
    """w0 = a + b σ √(1-ρ²)  ⇔  a = w0 - b σ √(1-ρ²)."""
    s = math.sqrt(1.0 - RHO * RHO)
    w0 = A + B * SIG * s
    a_back = w0 - B * SIG * s
    assert w0 == pytest.approx(0.104)
    assert a_back == pytest.approx(A)
    ks = np.array([0.0, M, 0.3])
    got = sf._svi_w(ks, a_back, B, RHO, M, SIG)
    for i, k in enumerate(ks):
        assert float(got[i]) == pytest.approx(_gatheral_w(float(k), A, B, RHO, M, SIG))


def test_nonnegative_w_domain_is_w0() -> None:
    s = math.sqrt(1.0 - RHO * RHO)
    a_on_boundary = 0.0 - B * SIG * s  # w0 = 0
    k_star = M - RHO * SIG / s
    assert float(sf._svi_w(k_star, a_on_boundary, B, RHO, M, SIG)) == pytest.approx(0.0, abs=1e-15)
    a_negative = a_on_boundary - 1e-3
    assert float(sf._svi_w(k_star, a_negative, B, RHO, M, SIG)) < 0.0


# ---------------------------------------------------------------------------
# w' = b(ρ + d/sq),  w'' = b σ² / sq³
# ---------------------------------------------------------------------------


def test_wp_wpp_at_k_equals_m() -> None:
    """d = 0, sq = σ: w' = b ρ = -0.24, w'' = b/σ = 2."""
    wp, wpp = _gatheral_wp_wpp(M, B, RHO, M, SIG)
    assert wp == pytest.approx(B * RHO)
    assert wpp == pytest.approx(B / SIG)
    assert wp == pytest.approx(-0.24)
    assert wpp == pytest.approx(2.0)


def test_wp_wpp_at_k_equals_m_plus_sigma_exact_in_sqrt2() -> None:
    """k = 0.3, d = σ: sq = σ√2, w' = -0.24 + 0.2√2, w'' = √2/2."""
    k = M + SIG
    wp, wpp = _gatheral_wp_wpp(k, B, RHO, M, SIG)
    assert wp == pytest.approx(-0.24 + 0.2 * math.sqrt(2.0), rel=1e-15)
    assert wpp == pytest.approx(math.sqrt(2.0) / 2.0, rel=1e-15)
    # Sign of d/sq: the minus form b(ρ - d/sq) is -0.24 - 0.2√2 ≈ -0.523.
    wp_flipped = B * (RHO - (k - M) / math.sqrt((k - M) ** 2 + SIG**2))
    assert wp_flipped != pytest.approx(wp, abs=1e-6)
    assert wp_flipped == pytest.approx(-0.24 - 0.2 * math.sqrt(2.0), rel=1e-15)


def test_wp_wpp_match_finite_differences_of_svi_w() -> None:
    k = 0.3
    eps = 1e-6

    def w(x: float) -> float:
        return float(sf._svi_w(x, A, B, RHO, M, SIG))

    wp_fd = (w(k + eps) - w(k - eps)) / (2.0 * eps)
    wpp_fd = (w(k + eps) - 2.0 * w(k) + w(k - eps)) / (eps * eps)
    wp, wpp = _gatheral_wp_wpp(k, B, RHO, M, SIG)
    assert wp_fd == pytest.approx(wp, rel=1e-6, abs=1e-9)
    assert wpp_fd == pytest.approx(wpp, rel=1e-5, abs=1e-6)


def test_wing_slopes_are_b_times_one_plus_minus_rho() -> None:
    """Lee: dw/dk → b(1+ρ) as k→+∞ and b(ρ-1) as k→-∞."""
    far, eps = 1e6, 1.0

    def wp_fd(k: float) -> float:
        return (
            float(sf._svi_w(k + eps, A, B, RHO, M, SIG))
            - float(sf._svi_w(k - eps, A, B, RHO, M, SIG))
        ) / (2.0 * eps)

    assert wp_fd(far) == pytest.approx(B * (1.0 + RHO), rel=1e-8)
    assert wp_fd(-far) == pytest.approx(B * (RHO - 1.0), rel=1e-8)


# ---------------------------------------------------------------------------
# g(k) = (1 - k w'/(2w))² - (w'²/4)(1/w + 1/4) + w''/2
# ---------------------------------------------------------------------------


def test_g_at_k_equals_m_is_exact_2_0864() -> None:
    """At k = m = 0.1: w=0.12, w'=-0.24, w''=2 → g = 2.0864 exactly.

    Dropping 1/w leaves 2.09; dropping the 1/4 leaves 2.2064; substituting
    d = k-m for k in the first term leaves 1.8764. All three must miss.
    """
    g = _prod_g(M)
    assert g == pytest.approx(2.0864, abs=1e-12)
    assert _gatheral_g(M, A, B, RHO, M, SIG) == pytest.approx(2.0864, abs=1e-12)
    w, wp, wpp = 0.12, -0.24, 2.0
    quarter_only = (1.0 - M * wp / (2.0 * w)) ** 2 - (wp**2 / 4.0) * 0.25 + wpp / 2.0
    invw_only = (1.0 - M * wp / (2.0 * w)) ** 2 - (wp**2 / 4.0) * (1.0 / w) + wpp / 2.0
    d_for_k = (1.0 - 0.0 * wp / (2.0 * w)) ** 2 - (wp**2 / 4.0) * (1.0 / w + 0.25) + wpp / 2.0
    assert quarter_only == pytest.approx(2.2064, abs=1e-12)
    assert invw_only == pytest.approx(2.09, abs=1e-12)
    assert d_for_k == pytest.approx(1.8764, abs=1e-12)
    assert quarter_only != pytest.approx(g, abs=1e-6)
    assert invw_only != pytest.approx(g, abs=1e-6)
    assert d_for_k != pytest.approx(g, abs=1e-6)


def test_g_at_minimum_is_one_plus_half_wpp() -> None:
    """w'(k*) = 0, so g(k*) = 1 + w''/2. k* = 0.25, w'' = 1.024, g = 1.512."""
    k_star = 0.25
    assert _prod_g(k_star) == pytest.approx(1.512, abs=1e-12)


def test_g_matches_gatheral_at_k_with_nonzero_d() -> None:
    k = 0.3
    assert _prod_g(k) == pytest.approx(_gatheral_g(k, A, B, RHO, M, SIG), rel=1e-12)
    # Independent of the helper: plug FD derivatives into eq. (2.1).
    eps = 1e-6

    def w(x: float) -> float:
        return float(sf._svi_w(x, A, B, RHO, M, SIG))

    wp = (w(k + eps) - w(k - eps)) / (2.0 * eps)
    wpp = (w(k + eps) - 2.0 * w(k) + w(k - eps)) / (eps * eps)
    g_fd = (1.0 - k * wp / (2.0 * w(k))) ** 2 - (wp**2 / 4.0) * (1.0 / w(k) + 1.0 / 4.0) + wpp / 2.0
    assert _prod_g(k) == pytest.approx(g_fd, rel=1e-5, abs=1e-7)


def test_vogt_example_has_negative_g() -> None:
    """Published butterfly-arbitrageable smile: g(1) < 0 (Figure 1 of the paper)."""
    g1 = _prod_g(1.0, **VOGT)
    assert g1 == pytest.approx(_gatheral_g(1.0, **VOGT), rel=1e-12)
    assert g1 == pytest.approx(-0.027741695904626, rel=1e-9)
    assert g1 < 0.0
    with pytest.raises(sf.SurfaceArbitrageError, match="butterfly"):
        sf._check_butterfly(VOGT["a"], VOGT["b"], VOGT["rho"], VOGT["m"], VOGT["sigma"], 0.0, 0.2)


def test_g_sign_matches_black_scholes_butterfly() -> None:
    """Lemma 2.2: K ∂²C/∂K² has the sign of g (r = q = 0, T = 1, F = 100).

    Gatheral writes p(k) = ∂²C/∂K² = g/√(2πw) exp(-d₋²/2); the closed form
    is the density in log-moneyness, i.e. K · ∂²C/∂K². Magnitude pins the
    1/w + 1/4 mix; sign pins butterfly.
    """
    F, T, r = 100.0, 1.0, 0.0
    for k, params, want_pos in (
        (M, {"a": A, "b": B, "rho": RHO, "m": M, "sigma": SIG}, True),
        (0.3, {"a": A, "b": B, "rho": RHO, "m": M, "sigma": SIG}, True),
        (1.0, VOGT, False),
    ):
        K = F * math.exp(k)
        h = 1e-4 * K

        def call_at(strike: float, p: dict = params) -> float:
            ww = max(_gatheral_w(math.log(strike / F), **p), 1e-12)
            return float(price(F, strike, T, r, math.sqrt(ww / T), "call", q=0.0))

        d2 = (call_at(K + h) - 2.0 * call_at(K) + call_at(K - h)) / (h * h)
        w = float(
            sf._svi_w(k, params["a"], params["b"], params["rho"], params["m"], params["sigma"])
        )
        g = _prod_g(k, **params)
        dminus = -k / math.sqrt(w) - math.sqrt(w) / 2.0
        density_k = g / math.sqrt(2.0 * math.pi * w) * math.exp(-0.5 * dminus**2)
        assert (d2 > 0) == want_pos
        assert (g > 0) == want_pos
        assert K * d2 == pytest.approx(density_k, rel=1e-3, abs=1e-6)


# ---------------------------------------------------------------------------
# Jacobian of w wrt p = (w0, b, ρ, m, σ) with a = w0 - b σ √(1-ρ²) folded in
# ---------------------------------------------------------------------------


def test_jacobian_columns_match_hand_derivatives() -> None:
    k = 0.3
    w0 = A + B * SIG * math.sqrt(1.0 - RHO * RHO)
    p = np.array([w0, B, RHO, M, SIG])
    jac = sf._svi_w_jac(np.array([k]), p)[0]
    sqrt2 = math.sqrt(2.0)
    # ∂w/∂w0 = 1
    assert jac[0] == pytest.approx(1.0)
    # ∂w/∂b = ρ d + sq - σ √(1-ρ²) = √2/5 - 0.28
    assert jac[1] == pytest.approx(sqrt2 / 5.0 - 0.28, rel=1e-15)
    # Forgetting to fold a would leave ρd+sq = √2/5 - 0.12, off by σ√(1-ρ²)=0.16.
    unfolded_b = RHO * (k - M) + math.sqrt((k - M) ** 2 + SIG**2)
    assert unfolded_b != pytest.approx(jac[1], abs=1e-6)
    # ∂w/∂ρ = b d + b σ ρ / √(1-ρ²) = 0.02 exactly
    assert jac[2] == pytest.approx(0.02, abs=1e-15)
    # ∂w/∂m = b(-ρ - d/sq) = -w' = 0.24 - 0.2√2
    assert jac[3] == pytest.approx(0.24 - 0.2 * sqrt2, rel=1e-15)
    # ∂w/∂σ = b σ/sq - b √(1-ρ²) = 0.2√2 - 0.32
    assert jac[4] == pytest.approx(0.2 * sqrt2 - 0.32, rel=1e-15)
    unfolded_sigma = B * SIG / math.sqrt((k - M) ** 2 + SIG**2)
    assert unfolded_sigma != pytest.approx(jac[4], abs=1e-6)


def test_jacobian_columns_match_independent_closed_form_on_a_grid() -> None:
    w0 = A + B * SIG * math.sqrt(1.0 - RHO * RHO)
    p = np.array([w0, B, RHO, M, SIG])
    ks = np.linspace(-0.4, 0.4, 17)
    jac = sf._svi_w_jac(ks, p)
    s = math.sqrt(1.0 - RHO * RHO)
    for i, k in enumerate(ks):
        d = k - M
        q = math.sqrt(d * d + SIG * SIG)
        row = [
            1.0,
            RHO * d + q - SIG * s,
            B * d + B * SIG * RHO / s,
            B * (-RHO - d / q),
            B * SIG / q - B * s,
        ]
        assert jac[i] == pytest.approx(row, rel=1e-15, abs=1e-15)


# ---------------------------------------------------------------------------
# Slice.vol(K) = √(w(log(K/F)) / T)
# ---------------------------------------------------------------------------


def test_slice_vol_is_sqrt_w_over_t() -> None:
    T, F = 0.25, 100.0
    sl = _slice("2026-09-25", T, F, A, B, RHO, M, SIG)
    # K such that k = m: w = 0.12, vol = √(0.12 / 0.25) = √0.48
    K = F * math.exp(M)
    assert sl.vol(K) == pytest.approx(math.sqrt(0.12 / T), rel=1e-15)
    K2 = F * math.exp(0.3)
    w = _gatheral_w(0.3, A, B, RHO, M, SIG)
    assert sl.vol(K2) == pytest.approx(math.sqrt(w / T), rel=1e-14)


def test_slice_vol_uses_log_moneyness_not_raw_strike() -> None:
    sl = _slice("2026-09-25", 0.25, 100.0, A, B, RHO, M, SIG)
    # log(K/F) = 0 vs K - F = 0 happen to agree at K = F, but not the slope.
    at_fwd = sl.vol(100.0)
    slightly_up = sl.vol(101.0)
    k_up = math.log(101.0 / 100.0)
    w_up = _gatheral_w(k_up, A, B, RHO, M, SIG)
    assert slightly_up == pytest.approx(math.sqrt(w_up / 0.25), rel=1e-14)
    assert slightly_up != pytest.approx(at_fwd)


# ---------------------------------------------------------------------------
# Term interpolation: linear in total variance; vol-flat outside
# ---------------------------------------------------------------------------


def test_interpolation_is_linear_in_total_variance_not_vol() -> None:
    """Gatheral calendar interpolant: w(T) linear, vol = √(w/T)."""
    t_lo, t_hi = 0.25, 1.0
    # Flat slices, a = σ² T so vols are 0.20 and 0.24.
    a_lo, a_hi = 0.20**2 * t_lo, 0.24**2 * t_hi
    surf = sf.Surface(
        DAY,
        "SPXW",
        [
            _slice("2026-11-20", t_lo, 100.0, a_lo),
            _slice("2027-08-20", t_hi, 100.0, a_hi),
        ],
    )
    alpha = 0.25
    t = t_lo + alpha * (t_hi - t_lo)
    w = a_lo + alpha * (a_hi - a_lo)
    vol_w = math.sqrt(w / t)
    vol_sigma = (1.0 - alpha) * 0.20 + alpha * 0.24
    vol_variance = math.sqrt((1.0 - alpha) * 0.20**2 + alpha * 0.24**2)
    assert vol_sigma != pytest.approx(vol_w, rel=1e-4)
    assert vol_variance != pytest.approx(vol_w, rel=1e-4)
    assert surf.vol(100.0, t) == pytest.approx(vol_w, rel=1e-14)
    # The interpolant in total variance is linear in T.
    assert surf.vol(100.0, t) ** 2 * t == pytest.approx(w, rel=1e-14)


def test_interpolation_uses_each_slice_own_forward() -> None:
    t_lo, t_hi = 0.25, 0.50
    lo = _slice("2026-11-20", t_lo, 100.0, A, B, RHO, M, SIG)
    hi = _slice("2027-02-19", t_hi, 110.0, A + 0.04, B, RHO, M, SIG)
    surf = sf.Surface(DAY, "SPXW", [lo, hi])
    K, t = 100.0, 0.375  # midpoint in T
    w_lo = lo.total_variance(math.log(K / lo.forward))
    w_hi = hi.total_variance(math.log(K / hi.forward))
    w_same_f = hi.total_variance(math.log(K / lo.forward))
    assert w_hi != pytest.approx(w_same_f, rel=1e-6)
    w = 0.5 * (w_lo + w_hi)
    assert surf.vol(K, t) == pytest.approx(math.sqrt(w / t), rel=1e-14)


def test_extrapolation_holds_vol_flat_not_total_variance() -> None:
    t_lo, t_hi = 0.25, 1.0
    a_lo, a_hi = 0.04, 0.10
    surf = sf.Surface(
        DAY,
        "SPXW",
        [
            _slice("2026-11-20", t_lo, 100.0, a_lo),
            _slice("2027-08-20", t_hi, 100.0, a_hi),
        ],
    )
    assert surf.vol(100.0, 0.10) == surf.vol(100.0, t_lo)
    assert surf.vol(100.0, 2.00) == surf.vol(100.0, t_hi)
    # Vol-flat ⇒ w = vol² T grows with T; holding w flat would keep w = a_lo.
    assert surf.vol(100.0, 0.10) ** 2 * 0.10 != pytest.approx(a_lo)
    assert surf.vol(100.0, 0.10) ** 2 * 0.10 == pytest.approx(a_lo * 0.10 / t_lo)


def test_vol_on_a_fitted_expiry_matches_that_slice() -> None:
    t_lo, t_mid, t_hi = 0.25, 0.50, 1.0
    slices = [
        _slice("2026-11-20", t_lo, 100.0, 0.04),
        _slice("2027-02-19", t_mid, 100.0, 0.07),
        _slice("2027-08-20", t_hi, 100.0, 0.10),
    ]
    surf = sf.Surface(DAY, "SPXW", slices)
    for t, sl in zip((t_lo, t_mid, t_hi), slices, strict=True):
        assert surf.vol(105.0, t) == sl.vol(105.0)


# ---------------------------------------------------------------------------
# Calendar: w(k, T) non-decreasing in T (Gatheral Lemma 2.1)
# ---------------------------------------------------------------------------


def test_calendar_guard_is_on_total_variance_not_vol() -> None:
    """Vol down, total variance up is calendar-clean; the reverse is not."""
    # σ_near=0.20, T=0.25 → w=0.01; σ_far=0.15, T=1 → w=0.0225.
    clean = [
        _slice("2026-11-20", 0.25, 100.0, 0.20**2 * 0.25),
        _slice("2027-08-20", 1.00, 100.0, 0.15**2 * 1.00),
    ]
    sf.Surface(DAY, "SPXW", clean)  # must not raise
    dirty = [
        _slice("2026-11-20", 0.25, 100.0, 0.30**2 * 0.25),
        _slice("2027-08-20", 1.00, 100.0, 0.10**2 * 1.00),
    ]
    with pytest.raises(sf.SurfaceArbitrageError, match="calendar"):
        sf.Surface(DAY, "SPXW", dirty)


def test_interpolated_total_variance_is_nondecreasing_in_t() -> None:
    t_lo, t_hi = 0.25, 1.0
    surf = sf.Surface(
        DAY,
        "SPXW",
        [
            _slice("2026-11-20", t_lo, 100.0, 0.04, B, RHO, M, SIG),
            _slice("2027-08-20", t_hi, 100.0, 0.10, B, RHO, M, SIG),
        ],
    )
    ts = np.linspace(0.05, 1.5, 40)
    K = 100.0
    ws = [surf.vol(K, float(t)) ** 2 * float(t) for t in ts]
    assert all(ws[i + 1] >= ws[i] - 1e-14 for i in range(len(ws) - 1))
