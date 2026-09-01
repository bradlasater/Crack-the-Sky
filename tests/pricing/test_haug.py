"""Haug-style published tables (QuantLib europeanoption.cpp, Haug 1998).

Conventions named on every assertion: spot delta, vega per 1.00 vol, theta
per year (and per calendar day / 365).
"""

from __future__ import annotations

import pytest

from pricing.bsm import greeks, price, raw_greeks
from pricing.conventions import GreeksConventions

SPOT_YEAR = GreeksConventions(
    vega_unit="per_1.00",
    theta_unit="per_year",
    delta_kind="spot",
    gamma_kind="spot",
)
SPOT_CALENDAR_DAY = GreeksConventions(
    vega_unit="per_1.00",
    theta_unit="per_calendar_day",
    delta_kind="spot",
    gamma_kind="spot",
    calendar_days=365,
)


# type, K, S, q, r, T, vol, value   -- Haug 1998 pag 2-8, tol 1e-4
HAUG_PRICES = [
    ("call", 65.00, 60.00, 0.00, 0.08, 0.25, 0.30, 2.1334),
    ("put", 95.00, 100.00, 0.05, 0.10, 0.50, 0.20, 2.4648),
    ("put", 19.00, 19.00, 0.10, 0.10, 0.75, 0.28, 1.7011),
    ("call", 19.00, 19.00, 0.10, 0.10, 0.75, 0.28, 1.7011),
    ("call", 1.60, 1.56, 0.08, 0.06, 0.50, 0.12, 0.0291),
    ("put", 70.00, 75.00, 0.05, 0.10, 0.50, 0.35, 4.0870),
    ("call", 40.00, 42.00, 0.08, 0.04, 0.75, 0.35, 5.0975),
]


@pytest.mark.parametrize("cp,K,S,q,r,T,vol,value", HAUG_PRICES)
def test_haug_prices(cp, K, S, q, r, T, vol, value) -> None:
    got = price(S, K, T, r, vol, cp, q=q)
    assert got == pytest.approx(value, abs=1e-4)


def test_haug_delta() -> None:
    # Haug pag 11: S=105, K=100, q=r=0.10, T=0.5, vol=0.36
    gc = greeks(105, 100, 0.5, 0.10, 0.36, "call", q=0.10, conventions=SPOT_YEAR)
    gp = greeks(105, 100, 0.5, 0.10, 0.36, "put", q=0.10, conventions=SPOT_YEAR)
    assert gc.delta == pytest.approx(0.5946, abs=5e-5)
    assert gp.delta == pytest.approx(-0.3566, abs=5e-5)


def test_haug_elasticity() -> None:
    raw = raw_greeks(105, 100, 0.5, 0.10, 0.36, "put", q=0.10)
    assert raw["elasticity"] == pytest.approx(-4.8775, abs=5e-4)


def test_haug_gamma() -> None:
    gc = greeks(55, 60, 0.75, 0.10, 0.30, "call", q=0.00, conventions=SPOT_YEAR)
    gp = greeks(55, 60, 0.75, 0.10, 0.30, "put", q=0.00, conventions=SPOT_YEAR)
    assert gc.gamma == pytest.approx(0.0278, abs=5e-5)
    assert gp.gamma == pytest.approx(0.0278, abs=5e-5)


def test_haug_vega_per_1_00() -> None:
    gc = greeks(55, 60, 0.75, 0.10, 0.30, "call", q=0.00, conventions=SPOT_YEAR)
    gp = greeks(55, 60, 0.75, 0.10, 0.30, "put", q=0.00, conventions=SPOT_YEAR)
    assert gc.vega == pytest.approx(18.9358, abs=5e-4)
    assert gp.vega == pytest.approx(18.9358, abs=5e-4)


def test_haug_theta_per_year_and_per_day() -> None:
    # Put S=430 K=405 q=0.05 r=0.07 T=1/12 vol=0.20
    year = greeks(430, 405, 1.0 / 12.0, 0.07, 0.20, "put", q=0.05, conventions=SPOT_YEAR)
    day = greeks(430, 405, 1.0 / 12.0, 0.07, 0.20, "put", q=0.05, conventions=SPOT_CALENDAR_DAY)
    assert year.theta == pytest.approx(-31.1924, abs=5e-4)
    assert day.theta == pytest.approx(-0.0855, abs=5e-5)
    assert day.theta == pytest.approx(year.theta / 365.0, rel=1e-12)


def test_haug_rho() -> None:
    gc = greeks(72, 75, 1.0, 0.09, 0.19, "call", q=0.00, conventions=SPOT_YEAR)
    assert gc.rho == pytest.approx(38.7325, abs=5e-4)


def test_haug_dividend_rho() -> None:
    gp = greeks(500, 490, 0.25, 0.08, 0.15, "put", q=0.05, conventions=SPOT_YEAR)
    assert gp.rho_dividend == pytest.approx(42.2254, abs=5e-4)
