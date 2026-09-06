"""Independent re-derivation of every CRR and bump-and-revalue formula.

Hand-expanded n=2/n=3 trees pin u, d, a, p, disc, payoffs, continuation, and
the American max. A smooth stand-in for ``crr_price`` pins each finite-difference
stencil by the name it has in the BSM catalog. Changing a coefficient, sign, or
exponent in ``pricing.engine`` must fail one of these.
"""

from __future__ import annotations

import math

import pytest

from pricing.bsm import price as bsm_price
from pricing.conventions import GreeksConventions
from pricing.engine import AmericanCRR, crr_price

CONV = GreeksConventions(
    vega_unit="per_1.00",
    theta_unit="per_year",
    delta_kind="spot",
    gamma_kind="spot",
)

# Frozen n=2/n=3 values from an independent math.exp expansion (not numpy).
# They move in the 3rd digit or more if u=exp(σ dt), disc=1/(1+r dt),
# p=(a-u)/(u-d), a=exp(r dt) (dropping q), or the American max is skipped.
_N2_EU_CALL = 9.540501338582947  # S=K=100, T=1, r=0.05, σ=0.20, q=0
_N2_EU_PUT_R10 = 2.803440723204769  # S=K=100, T=1, r=0.10, σ=0.20, q=0
_N2_AM_PUT_R10 = 4.44863423297894
_N3_EU_CALL_Q = 7.779442707090966  # S=100, K=105, T=0.75, r=0.06, σ=0.25, q=0.03
_N2_AM_CALL_Q = 12.43317921079342  # S=100, K=90, T=1, r=0.05, σ=0.20, q=0.08
_N2_EU_CALL_Q = 11.510607277503771


def _catalog(monkeypatch: pytest.MonkeyPatch, fn):  # noqa: ANN001, ANN202
    """Bump-and-revalue through a stand-in whose derivatives are known exactly."""

    def fake(S, K, T, r, sigma, call_put, *, q, n_steps, american):  # noqa: ANN001, ANN202
        return float(fn(S, K, T, r, sigma, q))

    monkeypatch.setattr("pricing.engine.crr_price", fake)
    return AmericanCRR(n_steps=21).greeks(
        100.0, 100.0, 0.5, 0.05, 0.20, "call", q=0.03, conventions=CONV
    )


# ---------------------------------------------------------------------------
# Hand trees
# ---------------------------------------------------------------------------


def test_n2_european_call_hand_tree() -> None:
    """dt=T/n, u=exp(σ√dt), d=1/u, a=exp((r-q)dt), p=(a-d)/(u-d), disc=exp(-r dt)."""
    S, K, T, r, sigma, q, n = 100.0, 100.0, 1.0, 0.05, 0.20, 0.0, 2
    dt = T / n
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    a = math.exp((r - q) * dt)
    p = (a - d) / (u - d)
    disc = math.exp(-r * dt)
    assert 0.0 < p < 1.0
    # Recombining CRR: u d = 1, so the middle terminal node is S.
    Suu, Sud, Sdd = S * u * u, S * u * d, S * d * d
    assert Sud == pytest.approx(S, abs=1e-12)
    cuu, cud, cdd = max(Suu - K, 0.0), max(Sud - K, 0.0), max(Sdd - K, 0.0)
    cu = disc * (p * cuu + (1.0 - p) * cud)
    cd = disc * (p * cud + (1.0 - p) * cdd)
    root = disc * (p * cu + (1.0 - p) * cd)
    got = crr_price(S, K, T, r, sigma, "call", q=q, n_steps=n, american=False)
    assert got == pytest.approx(root, abs=1e-12)
    assert got == pytest.approx(_N2_EU_CALL, abs=1e-12)


def test_n2_european_put_payoff_is_the_put_analog() -> None:
    S, K, T, r, sigma, q, n = 100.0, 100.0, 1.0, 0.10, 0.20, 0.0, 2
    dt = T / n
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    a = math.exp((r - q) * dt)
    p = (a - d) / (u - d)
    disc = math.exp(-r * dt)
    puu = max(K - S * u * u, 0.0)
    pud = max(K - S * u * d, 0.0)
    pdd = max(K - S * d * d, 0.0)
    pu = disc * (p * puu + (1.0 - p) * pud)
    pd = disc * (p * pud + (1.0 - p) * pdd)
    root = disc * (p * pu + (1.0 - p) * pd)
    got = crr_price(S, K, T, r, sigma, "put", q=q, n_steps=n, american=False)
    assert got == pytest.approx(root, abs=1e-12)
    assert got == pytest.approx(_N2_EU_PUT_R10, abs=1e-12)


def test_n2_american_put_exercises_at_the_interior_down_node() -> None:
    """V = max(continuation, intrinsic) at every node, not just the root."""
    S, K, T, r, sigma, q, n = 100.0, 100.0, 1.0, 0.10, 0.20, 0.0, 2
    dt = T / n
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    a = math.exp((r - q) * dt)
    p = (a - d) / (u - d)
    disc = math.exp(-r * dt)
    puu = max(K - S * u * u, 0.0)
    pud = max(K - S * u * d, 0.0)
    pdd = max(K - S * d * d, 0.0)
    pu_cont = disc * (p * puu + (1.0 - p) * pud)
    pd_cont = disc * (p * pud + (1.0 - p) * pdd)
    pu = max(pu_cont, K - S * u)
    pd = max(pd_cont, K - S * d)
    assert pd > pd_cont  # exercise on the down node
    assert pu == pytest.approx(pu_cont, abs=1e-12)
    root_cont = disc * (p * pu + (1.0 - p) * pd)
    root = max(root_cont, K - S)
    assert root == pytest.approx(root_cont, abs=1e-12)  # ATM root: max does not bind
    # Skipping the interior max recovers the European price, not this one.
    assert abs(root - _N2_EU_PUT_R10) > 1e-8
    got = crr_price(S, K, T, r, sigma, "put", q=q, n_steps=n, american=True)
    assert got == pytest.approx(root, abs=1e-12)
    assert got == pytest.approx(_N2_AM_PUT_R10, abs=1e-12)


def test_n3_european_call_pins_q_in_the_growth_factor() -> None:
    """a = exp((r-q)dt), not exp(r dt). Terminal spots S u^j d^{n-j}."""
    S, K, T, r, sigma, q, n = 100.0, 105.0, 0.75, 0.06, 0.25, 0.03, 3
    dt = T / n
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    a = math.exp((r - q) * dt)
    p = (a - d) / (u - d)
    disc = math.exp(-r * dt)
    # j = 0,1,2,3 up-moves.
    pay = [max(S * (u**j) * (d ** (n - j)) - K, 0.0) for j in range(n + 1)]
    v2 = [
        disc * (p * pay[1] + (1.0 - p) * pay[0]),
        disc * (p * pay[2] + (1.0 - p) * pay[1]),
        disc * (p * pay[3] + (1.0 - p) * pay[2]),
    ]
    v1 = [
        disc * (p * v2[1] + (1.0 - p) * v2[0]),
        disc * (p * v2[2] + (1.0 - p) * v2[1]),
    ]
    v0 = disc * (p * v1[1] + (1.0 - p) * v1[0])
    assert v0 == pytest.approx(_N3_EU_CALL_Q, abs=1e-12)
    got = crr_price(S, K, T, r, sigma, "call", q=q, n_steps=n, american=False)
    assert got == pytest.approx(_N3_EU_CALL_Q, abs=1e-12)


def test_n2_american_call_with_dividend_exercises_early() -> None:
    S, K, T, r, sigma, q, n = 100.0, 90.0, 1.0, 0.05, 0.20, 0.08, 2
    amer = crr_price(S, K, T, r, sigma, "call", q=q, n_steps=n, american=True)
    euro = crr_price(S, K, T, r, sigma, "call", q=q, n_steps=n, american=False)
    assert amer == pytest.approx(_N2_AM_CALL_Q, abs=1e-12)
    assert euro == pytest.approx(_N2_EU_CALL_Q, abs=1e-12)
    assert amer > euro


def test_q0_american_call_equals_european_on_a_tiny_tree() -> None:
    """A non-dividend call is never exercised early; the max does not bind."""
    amer = crr_price(100.0, 100.0, 1.0, 0.05, 0.20, "call", q=0.0, n_steps=2, american=True)
    euro = crr_price(100.0, 100.0, 1.0, 0.05, 0.20, "call", q=0.0, n_steps=2, american=False)
    assert amer == pytest.approx(euro, abs=1e-12)
    assert amer == pytest.approx(_N2_EU_CALL, abs=1e-12)


def test_american_put_premium_vs_same_tree_european() -> None:
    """r>0: American put > European CRR on the same tree, not just > BSM."""
    S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.10, 0.20
    amer = crr_price(S, K, T, r, sigma, "put", q=0.0, n_steps=2, american=True)
    euro = crr_price(S, K, T, r, sigma, "put", q=0.0, n_steps=2, american=False)
    assert amer == pytest.approx(_N2_AM_PUT_R10, abs=1e-12)
    assert euro == pytest.approx(_N2_EU_PUT_R10, abs=1e-12)
    assert amer > euro + 1.0


def test_crr_price_defaults_to_american() -> None:
    amer = crr_price(100.0, 100.0, 1.0, 0.10, 0.20, "put", q=0.0, n_steps=2)
    assert amer == pytest.approx(_N2_AM_PUT_R10, abs=1e-12)


def test_european_put_call_parity_holds_exactly() -> None:
    """Pins disc=exp(-r dt) together with a=exp((r-q)dt) via the RN measure."""
    S, K, T, r, sigma, q, n = 100.0, 105.0, 0.75, 0.06, 0.25, 0.03, 3
    call = crr_price(S, K, T, r, sigma, "call", q=q, n_steps=n, american=False)
    put = crr_price(S, K, T, r, sigma, "put", q=q, n_steps=n, american=False)
    forward = S * math.exp(-q * T) - K * math.exp(-r * T)
    assert call - put == pytest.approx(forward, abs=1e-12)


def test_european_crr_converges_to_bsm() -> None:
    S, K, T, r, sigma, q = 100.0, 100.0, 0.5, 0.05, 0.20, 0.0
    bsm = float(bsm_price(S, K, T, r, sigma, "call", q=q))
    errs = []
    for n in (21, 51, 101):
        px = crr_price(S, K, T, r, sigma, "call", q=q, n_steps=n, american=False)
        errs.append(abs(px - bsm) / bsm)
    assert errs[0] > errs[1] > errs[2]
    assert errs[2] < 5e-3


def test_deep_itm_american_put_is_immediate_exercise() -> None:
    S, K = 50.0, 100.0
    px = crr_price(S, K, 0.5, 0.05, 0.20, "put", q=0.0, n_steps=21, american=True)
    assert px == pytest.approx(K - S, abs=1e-12)


# ---------------------------------------------------------------------------
# Arbitrage-free condition: σ ≥ |r-q| √dt  (p=0 or 1 is allowed, never clipped)
# ---------------------------------------------------------------------------


def test_arb_free_boundary_is_abs_r_minus_q_sqrt_dt() -> None:
    """p ∈ [0,1] iff d ≤ exp((r-q)dt) ≤ u iff σ ≥ |r-q|√dt. Equality is p=0 or 1."""
    T, n = 1.0, 50
    dt = T / n
    for r, q in ((0.5, 0.0), (0.0, 0.5)):
        floor = abs(r - q) * math.sqrt(dt)
        with pytest.raises(ValueError, match="not arbitrage-free"):
            crr_price(100.0, 100.0, T, r, floor * 0.99, "call", q=q, n_steps=n)
        # p = 0 or 1 is allowed (never clipped). A p=0 call can be worth 0.
        px = crr_price(100.0, 100.0, T, r, floor, "put", q=q, n_steps=n, american=False)
        assert px >= 0.0


def test_error_quotes_the_crr_u_d_a_p() -> None:
    S, K, T, r, sigma, q, n = 100.0, 100.0, 1.0, 5.0, 0.01, 0.0, 4
    dt = T / n
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    a = math.exp((r - q) * dt)
    p = (a - d) / (u - d)
    assert not (0.0 <= p <= 1.0)
    with pytest.raises(ValueError) as exc:
        crr_price(S, K, T, r, sigma, "call", q=q, n_steps=n)
    msg = str(exc.value)
    assert f"{p:.6g}" in msg
    assert f"{d:.6g}" in msg
    assert f"{a:.6g}" in msg
    assert f"{u:.6g}" in msg


# ---------------------------------------------------------------------------
# Finite-difference stencils (BSM catalog names). Quadratics distinguish
# central from one-sided; mixed/time/third-order each have a dedicated pin.
# ---------------------------------------------------------------------------


def test_delta_is_central_in_spot(monkeypatch: pytest.MonkeyPatch) -> None:
    # V = S² → Δ = 2S. One-sided (V(S+h)-V(S))/h = 2S+h, off by hS=0.01.
    cat = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: S**2)
    assert cat.delta == pytest.approx(200.0, abs=1e-8)
    assert cat.gamma == pytest.approx(2.0, abs=1e-8)


def test_dual_delta_is_central_in_strike(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: K**2)
    assert cat.dual_delta == pytest.approx(200.0, abs=1e-8)
    assert cat.dual_gamma == pytest.approx(2.0, abs=1e-8)


def test_vega_volga_are_central_in_sigma(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: sigma**2)
    assert cat.vega == pytest.approx(0.40, abs=1e-7)  # 2σ; one-sided is 2σ+hs
    assert cat.volga == pytest.approx(2.0, abs=1e-7)


def test_rho_is_central_in_r(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: r**2)
    assert cat.rho == pytest.approx(0.10, abs=1e-8)  # 2r; one-sided is 2r+hr


def test_rho_dividend_is_central_in_q(monkeypatch: pytest.MonkeyPatch) -> None:
    """∂V/∂q, not −∂V/∂q."""
    cat = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: q**2)
    assert cat.rho_dividend == pytest.approx(0.06, abs=1e-8)


def test_vanna_four_point_mixed_stencil(monkeypatch: pytest.MonkeyPatch) -> None:
    """(V(S+hS,σ+hs) − V(S+hS,σ−hs) − V(S−hS,σ+hs) + V(S−hS,σ−hs)) / (4 hS hs)."""
    cat = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: S * sigma)
    assert cat.vanna == pytest.approx(1.0, abs=1e-8)
    # Denominator 2 hS hs would report 2; a two-point diagonal is ~ S/(2 hS).


def test_theta_is_backward_in_calendar_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """θ = (V(T−hT) − V(T)) / hT = ∂V/∂t, not ∂V/∂T."""
    cat = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: T)
    assert cat.theta == pytest.approx(-1.0, abs=1e-12)
    cat2 = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: T**2)
    # Backward on T²: −2T + hT. Central in T would be +2T with the opposite sign.
    assert cat2.theta == pytest.approx(-1.0, abs=1e-3)
    assert cat2.theta < 0.0


def test_charm_veta_color_are_backward_in_calendar_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    charm = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: S * T)
    assert charm.charm == pytest.approx(-1.0, abs=1e-8)
    veta = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: sigma * T)
    assert veta.veta == pytest.approx(-1.0, abs=1e-8)
    color = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: S**2 * T)
    assert color.color == pytest.approx(-2.0, abs=1e-4)


def test_vera_is_d_rho_d_sigma(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: r * sigma)
    assert cat.vera == pytest.approx(1.0, abs=1e-8)
    assert cat.rho == pytest.approx(0.20, abs=1e-10)


def test_speed_is_d_gamma_d_spot(monkeypatch: pytest.MonkeyPatch) -> None:
    # S^4 so a one-sided dγ/dS is 24S+12 hS, 0.12 away from the central 24S.
    cat = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: S**4)
    assert cat.gamma == pytest.approx(12.0 * 100.0**2, rel=1e-8)
    assert cat.speed == pytest.approx(24.0 * 100.0, abs=5e-2)


def test_zomma_is_d_gamma_d_sigma(monkeypatch: pytest.MonkeyPatch) -> None:
    # γ = 2σ², zomma = 4σ. One-sided in σ is 4σ+2 hs.
    cat = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: S**2 * sigma**2)
    assert cat.zomma == pytest.approx(0.80, abs=1e-4)


def test_ultima_is_third_derivative_in_sigma(monkeypatch: pytest.MonkeyPatch) -> None:
    """ultima = ∂³V/∂σ³ = (vega(σ+h) − 2 vega + vega(σ−h)) / h², not volga."""
    cat = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: sigma**3)
    assert cat.ultima == pytest.approx(6.0, abs=1e-3)
    assert cat.volga == pytest.approx(1.2, abs=1e-6)  # 6σ; first deriv of vega
    assert cat.vega == pytest.approx(0.12, abs=1e-8)  # 3σ²


def test_elasticity_is_delta_s_over_price(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _catalog(monkeypatch, lambda S, K, T, r, sigma, q: S)
    assert cat.delta == pytest.approx(1.0, abs=1e-12)
    assert cat.price == pytest.approx(100.0, abs=1e-12)
    assert cat.elasticity == pytest.approx(1.0, abs=1e-12)


# ---------------------------------------------------------------------------
# Real-tree Greeks: sign of calendar theta, elasticity identity
# ---------------------------------------------------------------------------


def test_atm_call_theta_is_negative() -> None:
    """A typical long call decays as T shrinks. Conventions: theta per year."""
    S, K, T, r, sigma = 100.0, 100.0, 0.5, 0.05, 0.20
    cat = AmericanCRR(n_steps=21).greeks(S, K, T, r, sigma, "call", q=0.0, conventions=CONV)
    assert cat.theta < 0.0
    hT = 1e-4 * T
    later = crr_price(S, K, T - hT, r, sigma, "call", q=0.0, n_steps=21, american=True)
    assert later < cat.price
    # The engine's backward stencil is (V(T−hT)−V(T))/hT, not (V(T+hT)−V(T))/hT.
    assert cat.theta == pytest.approx((later - cat.price) / hT, rel=1e-6, abs=1e-8)


def test_tree_elasticity_matches_delta_s_over_price() -> None:
    cat = AmericanCRR(n_steps=21).greeks(
        100.0, 100.0, 0.5, 0.05, 0.20, "call", q=0.0, conventions=CONV
    )
    assert cat.price > 0.0
    assert cat.elasticity == pytest.approx(cat.delta * 100.0 / cat.price, rel=1e-12)
