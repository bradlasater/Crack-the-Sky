"""Vega/theta unit conversions and spot vs dual delta/gamma."""

from __future__ import annotations

import pytest

from pricing.bsm import greeks, raw_greeks
from pricing.conventions import GreeksConventions

S, K, T, R, Q, SIG = 100.0, 100.0, 0.5, 0.05, 0.02, 0.20

PER_100 = GreeksConventions(
    vega_unit="per_1.00", theta_unit="per_year", delta_kind="spot", gamma_kind="spot"
)
PER_1PCT = GreeksConventions(
    vega_unit="per_1pct", theta_unit="per_year", delta_kind="spot", gamma_kind="spot"
)
PER_DAY = GreeksConventions(
    vega_unit="per_1.00",
    theta_unit="per_calendar_day",
    delta_kind="spot",
    gamma_kind="spot",
    calendar_days=365,
)
PER_252 = GreeksConventions(
    vega_unit="per_1.00",
    theta_unit="per_trading_day",
    delta_kind="spot",
    gamma_kind="spot",
    trading_days=252,
)
DUAL = GreeksConventions(
    vega_unit="per_1.00", theta_unit="per_year", delta_kind="dual", gamma_kind="dual"
)


def test_vega_per_1pct_is_one_hundredth() -> None:
    a = greeks(S, K, T, R, SIG, "call", q=Q, conventions=PER_100)
    b = greeks(S, K, T, R, SIG, "call", q=Q, conventions=PER_1PCT)
    assert b.vega == pytest.approx(a.vega * 0.01, rel=1e-12)
    assert b.vanna == pytest.approx(a.vanna * 0.01, rel=1e-12)
    assert b.volga == pytest.approx(a.volga * 0.01**2, rel=1e-12)


def test_theta_calendar_and_trading_day() -> None:
    year = greeks(S, K, T, R, SIG, "call", q=Q, conventions=PER_100)
    day = greeks(S, K, T, R, SIG, "call", q=Q, conventions=PER_DAY)
    trad = greeks(S, K, T, R, SIG, "call", q=Q, conventions=PER_252)
    assert day.theta == pytest.approx(year.theta / 365.0, rel=1e-12)
    assert trad.theta == pytest.approx(year.theta / 252.0, rel=1e-12)
    assert day.charm == pytest.approx(year.charm / 365.0, rel=1e-12)


def test_dual_kind_swaps_delta_gamma_fields() -> None:
    raw = raw_greeks(S, K, T, R, SIG, "call", q=Q)
    spot = greeks(S, K, T, R, SIG, "call", q=Q, conventions=PER_100)
    dual = greeks(S, K, T, R, SIG, "call", q=Q, conventions=DUAL)
    assert spot.delta == pytest.approx(raw["delta"])
    assert dual.delta == pytest.approx(raw["dual_delta"])
    assert dual.gamma == pytest.approx(raw["dual_gamma"])
    assert dual.dual_delta == pytest.approx(raw["dual_delta"])
