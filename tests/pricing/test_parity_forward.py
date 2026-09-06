"""Exact pins for put-call-parity forwards and the shared median helper.

Changing 365 to 365.25, dropping ``e^{rT}`` when a rate was supplied, or
taking ``F = K + (P - C)`` must fail these.
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from ingest.jobs import _median, forward_from_parity


def _pair(
    exp: str, strike: float, call: float, put: float, *, underlying: str = "I:SPX"
) -> list[dict]:
    return [
        {
            "details_expiration_date": exp,
            "details_strike_price": strike,
            "details_contract_type": kind,
            "day_close": px,
            "underlying_ticker": underlying,
            "day_last_updated_ns": 1,
        }
        for kind, px in (("call", call), ("put", put))
    ]


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------


def test_picks_the_strike_minimising_abs_call_minus_put() -> None:
    exp = "2026-10-01"
    rows = (
        _pair(exp, 7600.0, 80.0, 20.0)       # |C-P| = 60
        + _pair(exp, 7690.0, 50.5, 49.5)     # |C-P| = 1  <-- winner
        + _pair(exp, 7800.0, 10.0, 90.0)     # |C-P| = 80
    )
    got = forward_from_parity(rows)[0]
    assert got["atm_strike"] == 7690.0
    assert got["call_price"] == 50.5
    assert got["put_price"] == 49.5
    assert got["pairs"] == 3


# ---------------------------------------------------------------------------
# Undiscounted: F = K + C - P
# ---------------------------------------------------------------------------


def test_undiscounted_parity_is_k_plus_call_minus_put() -> None:
    K, C, P = 7700.0, 51.25, 49.10
    got = forward_from_parity(_pair("2026-10-01", K, C, P))[0]
    assert got["forward"] == pytest.approx(K + C - P, rel=1e-12)
    assert got["method"] == "parity-r0"


def test_put_rich_spread_pulls_the_forward_below_the_strike() -> None:
    """Sign of (C - P) is load-bearing: F = K + (C - P), not K + (P - C)."""
    K, C, P = 7700.0, 40.0, 55.0
    got = forward_from_parity(_pair("2026-10-01", K, C, P))[0]
    assert got["forward"] == pytest.approx(K + C - P, rel=1e-12)
    assert got["forward"] < K
    assert got["forward"] != pytest.approx(K + P - C, rel=1e-9)


# ---------------------------------------------------------------------------
# Discounted: F = K + e^{rT}(C - P), T = max((expiry-asof).days, 0)/365
# ---------------------------------------------------------------------------


def test_discounted_parity_is_k_plus_exp_rt_times_spread() -> None:
    K, C, P, r = 7700.0, 51.25, 49.10, 0.0384
    asof, expiry = date(2026, 9, 1), date(2026, 10, 1)
    T = max((expiry - asof).days, 0) / 365.0
    assert (expiry - asof).days == 30
    expected = K + math.exp(r * T) * (C - P)

    got = forward_from_parity(
        _pair(expiry.isoformat(), K, C, P),
        rate_for_expiry=lambda _d: r,
        asof_date=asof,
    )[0]
    assert got["forward"] == pytest.approx(expected, rel=1e-12)
    assert got["method"] == "parity"
    # Dropping e^{rT} when a rate WAS supplied.
    assert got["forward"] != pytest.approx(K + (C - P), rel=1e-9)
    # ACT/365.25 would move T.
    T_wrong = max((expiry - asof).days, 0) / 365.25
    assert got["forward"] != pytest.approx(
        K + math.exp(r * T_wrong) * (C - P), rel=1e-12
    )


def test_discounted_put_rich_spread_uses_the_same_sign() -> None:
    K, C, P, r = 7700.0, 40.0, 55.0, 0.0384
    asof, expiry = date(2026, 9, 1), date(2026, 10, 1)
    T = max((expiry - asof).days, 0) / 365.0
    expected = K + math.exp(r * T) * (C - P)
    got = forward_from_parity(
        _pair(expiry.isoformat(), K, C, P),
        rate_for_expiry=lambda _d: r,
        asof_date=asof,
    )[0]
    assert got["forward"] == pytest.approx(expected, rel=1e-12)
    assert got["forward"] < K


def test_asof_on_or_after_expiry_clamps_T_to_zero() -> None:
    K, C, P, r = 7700.0, 51.25, 49.10, 0.05
    expiry = date(2026, 9, 1)
    for asof in (expiry, date(2026, 9, 2)):
        got = forward_from_parity(
            _pair(expiry.isoformat(), K, C, P),
            rate_for_expiry=lambda _d: r,
            asof_date=asof,
        )[0]
        assert got["forward"] == pytest.approx(K + C - P, rel=1e-12)


def test_zero_rate_matches_the_undiscounted_form() -> None:
    K, C, P = 7700.0, 51.25, 49.10
    rows = _pair("2026-10-01", K, C, P)
    a = forward_from_parity(rows)[0]["forward"]
    b = forward_from_parity(
        rows, rate_for_expiry=lambda _d: 0.0, asof_date=date(2026, 9, 1)
    )[0]["forward"]
    assert a == pytest.approx(b, rel=1e-12)
    assert a == pytest.approx(K + C - P, rel=1e-12)


# ---------------------------------------------------------------------------
# _median: standard statistical median
# ---------------------------------------------------------------------------


def test_median_empty_is_none() -> None:
    assert _median([]) is None


def test_median_odd_is_the_middle_of_the_sorted_values() -> None:
    assert _median([3.0, 1.0, 2.0]) == 2.0
    assert _median([7.0]) == 7.0
    assert _median([1.0, 10.0, 100.0]) == 10.0


def test_median_even_is_the_mean_of_the_two_middle() -> None:
    assert _median([4.0, 1.0, 2.0, 3.0]) == 2.5
    assert _median([1.0, 3.0]) == 2.0
    # Not the lower- or upper-only pick of the two middle values.
    assert _median([1.0, 2.0, 3.0, 4.0]) != 2.0
    assert _median([1.0, 2.0, 3.0, 4.0]) != 3.0
