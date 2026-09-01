"""Finite-difference vs analytic for every named Greek.

Conventions: spot delta/gamma, vega per 1.00 vol, theta per year of calendar
time (T decreasing). Tolerances loosen with derivative order.
"""

from __future__ import annotations

import numpy as np
import pytest

from pricing.bsm import price, raw_greeks
from pricing.conventions import GREEK_NAMES

S, K, T, R, Q, SIG = 100.0, 105.0, 0.75, 0.06, 0.03, 0.25
CP = "call"


def _p(**kw) -> float:
    args = {"S": S, "K": K, "T": T, "r": R, "sigma": SIG, "call_put": CP, "q": Q}
    args.update(kw)
    return float(price(**args))


def _g(name: str, **kw) -> float:
    args = {"S": S, "K": K, "T": T, "r": R, "sigma": SIG, "call_put": CP, "q": Q}
    args.update(kw)
    return float(raw_greeks(**args)[name])


def test_all_named_greeks_finite() -> None:
    raw = raw_greeks(S, K, T, R, SIG, CP, q=Q)
    for name in GREEK_NAMES:
        val = raw[name]
        assert np.isfinite(val), name


def test_delta_fd() -> None:
    h = 1e-4 * S
    fd = (_p(S=S + h) - _p(S=S - h)) / (2 * h)
    assert _g("delta") == pytest.approx(fd, rel=1e-6, abs=1e-7)


def test_dual_delta_fd() -> None:
    h = 1e-4 * K
    fd = (_p(K=K + h) - _p(K=K - h)) / (2 * h)
    assert _g("dual_delta") == pytest.approx(fd, rel=1e-6, abs=1e-7)


def test_vega_fd() -> None:
    h = 1e-5
    fd = (_p(sigma=SIG + h) - _p(sigma=SIG - h)) / (2 * h)
    assert _g("vega") == pytest.approx(fd, rel=1e-6, abs=1e-7)


def test_rho_fd() -> None:
    h = 1e-6
    fd = (_p(r=R + h) - _p(r=R - h)) / (2 * h)
    assert _g("rho") == pytest.approx(fd, rel=1e-5, abs=1e-7)


def test_rho_dividend_fd() -> None:
    h = 1e-6
    fd = (_p(q=Q + h) - _p(q=Q - h)) / (2 * h)
    assert _g("rho_dividend") == pytest.approx(fd, rel=1e-5, abs=1e-7)


def test_theta_fd_calendar() -> None:
    h = 1e-6
    fd = (_p(T=T - h) - _p(T=T)) / h
    assert _g("theta") == pytest.approx(fd, rel=1e-4, abs=1e-6)


def test_gamma_fd() -> None:
    h = 1e-3
    fd = (_p(S=S + h) - 2 * _p() + _p(S=S - h)) / (h**2)
    assert _g("gamma") == pytest.approx(fd, rel=1e-4, abs=1e-6)


def test_dual_gamma_fd() -> None:
    h = 1e-3
    fd = (_p(K=K + h) - 2 * _p() + _p(K=K - h)) / (h**2)
    assert _g("dual_gamma") == pytest.approx(fd, rel=1e-4, abs=1e-6)


def test_volga_fd() -> None:
    h = 1e-4
    fd = (_p(sigma=SIG + h) - 2 * _p() + _p(sigma=SIG - h)) / (h**2)
    assert _g("volga") == pytest.approx(fd, rel=1e-3, abs=1e-5)


def test_vanna_fd() -> None:
    hS, hs = 1e-3, 1e-4
    fd = (
        _p(S=S + hS, sigma=SIG + hs)
        - _p(S=S + hS, sigma=SIG - hs)
        - _p(S=S - hS, sigma=SIG + hs)
        + _p(S=S - hS, sigma=SIG - hs)
    ) / (4 * hS * hs)
    assert _g("vanna") == pytest.approx(fd, rel=1e-3, abs=1e-5)


def test_charm_fd() -> None:
    h = 1e-5
    fd = (_g("delta", T=T - h) - _g("delta")) / h
    assert _g("charm") == pytest.approx(fd, rel=1e-4, abs=1e-6)


def test_veta_fd() -> None:
    h = 1e-5
    fd = (_g("vega", T=T - h) - _g("vega")) / h
    assert _g("veta") == pytest.approx(fd, rel=1e-4, abs=1e-5)


def test_vera_fd() -> None:
    h = 1e-5
    fd = (_g("rho", sigma=SIG + h) - _g("rho", sigma=SIG - h)) / (2 * h)
    assert _g("vera") == pytest.approx(fd, rel=1e-4, abs=1e-6)


def test_speed_fd() -> None:
    h = 1e-3
    fd = (_g("gamma", S=S + h) - _g("gamma", S=S - h)) / (2 * h)
    assert _g("speed") == pytest.approx(fd, rel=1e-3, abs=1e-6)


def test_zomma_fd() -> None:
    h = 1e-4
    fd = (_g("gamma", sigma=SIG + h) - _g("gamma", sigma=SIG - h)) / (2 * h)
    assert _g("zomma") == pytest.approx(fd, rel=1e-3, abs=1e-6)


def test_color_fd() -> None:
    h = 1e-5
    fd = (_g("gamma", T=T - h) - _g("gamma")) / h
    assert _g("color") == pytest.approx(fd, rel=1e-3, abs=1e-6)


def test_ultima_fd() -> None:
    h = 1e-4
    fd = (_g("volga", sigma=SIG + h) - _g("volga", sigma=SIG - h)) / (2 * h)
    assert _g("ultima") == pytest.approx(fd, rel=1e-3, abs=1e-4)


def test_elasticity() -> None:
    raw = raw_greeks(S, K, T, R, SIG, CP, q=Q)
    assert raw["elasticity"] == pytest.approx(raw["delta"] * S / raw["price"], rel=1e-12)
