"""Put-call identities. Conventions: spot delta/gamma, vega per 1.00, theta per year."""

from __future__ import annotations

import numpy as np
import pytest

from pricing.bsm import price, raw_greeks
from pricing.conventions import GreeksConventions
from pricing.engine import EuropeanBSM

CONV = GreeksConventions(
    vega_unit="per_1.00",
    theta_unit="per_year",
    delta_kind="spot",
    gamma_kind="spot",
)

S, K, T, R, Q, SIG = 100.0, 100.0, 0.5, 0.05, 0.02, 0.20
ENG = EuropeanBSM()


def test_engine_protocol() -> None:
    assert ENG.name == "european_bsm"
    gc = ENG.greeks(S, K, T, R, SIG, "call", q=Q, conventions=CONV)
    assert gc.conventions is CONV


def test_delta_call_minus_put() -> None:
    gc = raw_greeks(S, K, T, R, SIG, "call", q=Q)
    gp = raw_greeks(S, K, T, R, SIG, "put", q=Q)
    assert gc["delta"] - gp["delta"] == pytest.approx(np.exp(-Q * T), rel=1e-12)


def test_gamma_vega_equal() -> None:
    gc = raw_greeks(S, K, T, R, SIG, "call", q=Q)
    gp = raw_greeks(S, K, T, R, SIG, "put", q=Q)
    for name in ("gamma", "vega", "volga", "vanna", "dual_gamma", "speed", "zomma", "ultima"):
        assert gc[name] == pytest.approx(gp[name], rel=1e-12, abs=1e-14), name


def test_dual_delta_identity() -> None:
    gc = raw_greeks(S, K, T, R, SIG, "call", q=Q)
    gp = raw_greeks(S, K, T, R, SIG, "put", q=Q)
    assert gc["dual_delta"] - gp["dual_delta"] == pytest.approx(-np.exp(-R * T), rel=1e-12)


def test_rho_identity() -> None:
    gc = raw_greeks(S, K, T, R, SIG, "call", q=Q)
    gp = raw_greeks(S, K, T, R, SIG, "put", q=Q)
    assert gc["rho"] - gp["rho"] == pytest.approx(K * T * np.exp(-R * T), rel=1e-12)
    assert gc["rho_dividend"] - gp["rho_dividend"] == pytest.approx(
        -T * S * np.exp(-Q * T), rel=1e-12
    )


def test_theta_identity() -> None:
    gc = raw_greeks(S, K, T, R, SIG, "call", q=Q)
    gp = raw_greeks(S, K, T, R, SIG, "put", q=Q)
    assert gc["theta"] - gp["theta"] == pytest.approx(
        Q * S * np.exp(-Q * T) - R * K * np.exp(-R * T), rel=1e-10, abs=1e-12
    )


def test_put_call_parity_price() -> None:
    c = price(S, K, T, R, SIG, "call", q=Q)
    p = price(S, K, T, R, SIG, "put", q=Q)
    assert c - p == pytest.approx(S * np.exp(-Q * T) - K * np.exp(-R * T), rel=1e-12)


def test_forward_equivalent_to_q() -> None:
    F = S * np.exp((R - Q) * T)
    from_q = price(S, K, T, R, SIG, "call", q=Q)
    from_f = price(S, K, T, R, SIG, "call", F=F)
    assert from_q == pytest.approx(from_f, rel=1e-12)


def test_q_and_f_together_is_error() -> None:
    with pytest.raises(ValueError, match="q or F"):
        price(S, K, T, R, SIG, "call", q=Q, F=100.0)
