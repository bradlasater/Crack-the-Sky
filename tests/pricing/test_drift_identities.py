"""Pinned formulas for the daily drift canary and remaining pricing-glue identities.

Each assertion uses a hand-computed expected value (literals, not the code's
own constants) so a silent coefficient / sign / exponent change fails.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pyarrow as pa
import pytest

from pricing import drift_check as drift_mod
from pricing import from_market
from pricing.bsm import price as bsm_price
from pricing.bsm import raw_greeks
from pricing.conventions import CALENDAR_DAYS_PER_YEAR
from pricing.drift_check import (
    DEFAULT_CUTOFF_ET,
    DEFAULT_THRESHOLDS,
    VENDOR_THETA_TO_YEAR,
    VENDOR_VEGA_TO_PER_1,
    Thresholds,
    align_vendor,
    beyond_band,
    cutoff_asof_ns,
    evaluate_drift,
    is_atm,
)

LOOSE = Thresholds(min_compare=1, fail_frac=0.25, atm_pct=0.05)
DT = date(2026, 8, 28)


def _euro_row(cp: str, **over: object) -> dict:
    """ATM European row whose stored price/greeks match BSM at the inputs."""
    S, K, T, rate, q, sig = 100.0, 100.0, 1.0, 0.05, 0.02, 0.20
    g = raw_greeks(S, K, T, rate, sig, cp, q=q)
    mkt = float(bsm_price(S, K, T, rate, sig, cp, q=q))
    token = "C" if cp == "call" else "P"
    row: dict = {
        "ticker": f"O:SPXW260918{token}00100000",
        "root": "SPXW",
        "expiry": "2026-09-18",
        "call_put": cp,
        "strike": K,
        "F": K,
        "S": S,
        "T": T,
        "r": rate,
        "q": q,
        "exercise_style": "european",
        "greeks_engine": "european_bsm",
        "own_iv": sig,
        "own_price": mkt,
        "own_delta": float(g["delta"]),
        "own_gamma": float(g["gamma"]),
        "own_vega": float(g["vega"]),
        "own_theta": float(g["theta"]),
        "market_price": mkt,
        "price_source": "close",
        "vendor_iv": None,
        "vendor_delta": None,
        "vendor_gamma": None,
        "vendor_vega": None,
        "vendor_theta": None,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# Vendor unit alignment
# ---------------------------------------------------------------------------


def test_vendor_unit_scales_match_from_market_and_are_365_and_100() -> None:
    """The two copies must not drift; 365→252 or 100→1 would desynchronize diffs."""
    assert VENDOR_THETA_TO_YEAR == from_market._VENDOR_THETA_TO_YEAR
    assert VENDOR_VEGA_TO_PER_1 == from_market._VENDOR_VEGA_TO_PER_1
    assert VENDOR_THETA_TO_YEAR == float(CALENDAR_DAYS_PER_YEAR) == 365.0
    assert VENDOR_VEGA_TO_PER_1 == 100.0


def test_align_vendor_is_theta_times_365_vega_times_100() -> None:
    aligned = align_vendor(
        {
            "vendor_theta": -2.0,
            "vendor_vega": 0.5,
            "vendor_iv": 0.16,
            "vendor_delta": 0.4,
            "vendor_gamma": 0.01,
        }
    )
    assert aligned["theta"] == pytest.approx(-2.0 * 365.0)
    assert aligned["vega"] == pytest.approx(0.5 * 100.0)
    # IV / delta / gamma are already in catalog units — not ×100 / ×365.
    assert aligned["iv"] == 0.16
    assert aligned["delta"] == 0.4
    assert aligned["gamma"] == 0.01


def test_from_market_max_trade_age_ns_is_minutes_times_60_times_1e9() -> None:
    minutes_to_ns = int(60.0 * 60 * 1e9)
    assert from_market.DEFAULT_MAX_TRADE_AGE_MIN == 60.0
    assert minutes_to_ns == 3_600_000_000_000
    assert minutes_to_ns == from_market.DEFAULT_MAX_TRADE_AGE_NS


# ---------------------------------------------------------------------------
# beyond_band: err > max(abs, rel * max(|own|, |vendor|, 1e-12))
# ---------------------------------------------------------------------------


def test_beyond_band_on_the_envelope_is_inside() -> None:
    """The comparison is strict `>`; landing exactly on the band is not beyond."""
    # Abs dominates: err = 1, max(1, 0.05*11) = 1.
    assert beyond_band(10.0, 11.0, abs_thr=1.0, rel_thr=0.05) is False
    assert beyond_band(10.0, 11.1, abs_thr=1.0, rel_thr=0.05) is True
    # Rel dominates: err = 10, max(1, 0.1*100) = 10.
    assert beyond_band(90.0, 100.0, abs_thr=1.0, rel_thr=0.1) is False
    assert beyond_band(89.0, 100.0, abs_thr=1.0, rel_thr=0.1) is True


def test_beyond_band_is_max_of_abs_and_rel_not_the_sum() -> None:
    # err = 12, abs = 2, rel*scale = 0.1*112 = 11.2.
    # 12 > max(2, 11.2) but 12 < 2 + 11.2, so abs+rel would wrongly pass.
    assert beyond_band(100.0, 112.0, abs_thr=2.0, rel_thr=0.1) is True


def test_beyond_band_scale_is_max_abs_not_the_sum() -> None:
    # err = 4, scale = max(10, 6) = 10, rel*scale = 3 → beyond.
    # If scale were |own|+|vendor| = 16, rel*scale = 4.8 and 4 would be inside.
    assert beyond_band(10.0, 6.0, abs_thr=0.1, rel_thr=0.3) is True


def test_beyond_band_scale_floor_is_1e_minus_12() -> None:
    assert beyond_band(0.0, 0.0, abs_thr=0.0, rel_thr=1.0) is False


def test_thr_for_maps_each_greek_onto_its_abs_rel_pair() -> None:
    t = DEFAULT_THRESHOLDS
    assert drift_mod._thr_for(t, "iv") == (0.04, 0.25)
    assert drift_mod._thr_for(t, "delta") == (0.08, 0.30)
    assert drift_mod._thr_for(t, "gamma") == (0.005, 0.75)
    assert drift_mod._thr_for(t, "vega") == (25.0, 0.50)
    assert drift_mod._thr_for(t, "theta") == (150.0, 0.60)


# ---------------------------------------------------------------------------
# is_atm: |K/F − 1| ≤ atm_pct
# ---------------------------------------------------------------------------


def test_is_atm_is_inclusive_strike_over_forward_moneyness() -> None:
    assert is_atm({"F": 100.0, "strike": 105.0}, 0.05) is True
    assert is_atm({"F": 100.0, "strike": 95.0}, 0.05) is True
    assert is_atm({"F": 100.0, "strike": 105.01}, 0.05) is False
    assert is_atm({"F": 100.0, "strike": 94.99}, 0.05) is False
    # |K/F − 1| = 0.10 vs |F/K − 1| ≈ 0.0909: the K/F form is outside 9.5%.
    assert is_atm({"F": 100.0, "strike": 110.0}, 0.095) is False
    # Absolute points |K − F| ≤ 0.05 would reject the 105/100 boundary above.
    assert is_atm({"F": 0.0, "strike": 100.0}, 0.05) is False
    assert is_atm({"F": -100.0, "strike": -100.0}, 0.05) is False
    assert is_atm({"F": None, "strike": 100.0}, 0.05) is False


# ---------------------------------------------------------------------------
# Put-call parity: S e^{-qT} − K e^{-rT}
# ---------------------------------------------------------------------------


def test_pcp_rhs_is_discounted_spot_minus_discounted_strike() -> None:
    S, K, T, r, q = 100.0, 90.0, 0.25, 0.05, 0.01
    got = drift_mod._pcp_rhs({"S": S, "strike": K, "T": T, "r": r, "q": q})
    expected = S * math.exp(-q * T) - K * math.exp(-r * T)
    assert got == pytest.approx(expected)
    assert got != pytest.approx(S - K)
    assert got != pytest.approx(S * math.exp(-r * T) - K * math.exp(-q * T))
    assert got != pytest.approx(S * (1.0 - q * T) - K * (1.0 - r * T))


def test_pcp_rhs_treats_missing_q_as_zero() -> None:
    S, K, T, r = 100.0, 90.0, 0.25, 0.05
    got = drift_mod._pcp_rhs({"S": S, "strike": K, "T": T, "r": r})
    assert got == pytest.approx(S - K * math.exp(-r * T))


def test_pcp_identity_uses_c_minus_p_not_s_minus_k() -> None:
    """S = K so S − K = 0, but r, q ≠ 0 so the discounted forward is not 0."""
    call, put = _euro_row("call"), _euro_row("put")
    rhs = 100.0 * math.exp(-0.02 * 1.0) - 100.0 * math.exp(-0.05 * 1.0)
    assert call["S"] == call["strike"]
    assert (call["market_price"] - put["market_price"]) == pytest.approx(rhs, rel=1e-12)
    report = evaluate_drift(
        pa.Table.from_pylist([call, put]), thresholds=LOOSE, dt=DT, asof_ns=1, r=0.05
    )
    assert report.beyond_by_identity["pcp"] == 0
    assert report.beyond_by_identity["gamma_pair"] == 0
    assert report.beyond_by_identity["vega_pair"] == 0
    assert report.status == "PASS"


def test_pcp_identity_fails_when_c_minus_p_leaves_the_band() -> None:
    call = _euro_row("call")
    put = _euro_row("put")
    call["market_price"] = call["own_price"] + 5.0
    call["own_price"] = call["market_price"]
    report = evaluate_drift(
        pa.Table.from_pylist([call, put]), thresholds=LOOSE, dt=DT, asof_ns=1, r=0.05
    )
    assert report.beyond_by_identity["pcp"] == 1
    assert report.beyond_by_identity["reprice"] == 0


def test_pcp_skipped_unless_both_legs_are_day_close() -> None:
    call = _euro_row("call", price_source="last")
    put = _euro_row("put")
    call["market_price"] = 99.0
    call["own_price"] = 99.0
    report = evaluate_drift(
        pa.Table.from_pylist([call, put]), thresholds=LOOSE, dt=DT, asof_ns=1, r=0.05
    )
    assert report.counts["atm_pairs"] == 1
    assert report.beyond_by_identity["pcp"] == 0


def test_american_pairs_skip_european_identities() -> None:
    call = _euro_row("call", exercise_style="american", greeks_engine="american_crr")
    put = _euro_row("put", exercise_style="american", greeks_engine="american_crr")
    call["market_price"] = 99.0
    call["own_price"] = 99.0
    report = evaluate_drift(
        pa.Table.from_pylist([call, put]), thresholds=LOOSE, dt=DT, asof_ns=1, r=0.05
    )
    assert report.counts["atm_pairs"] == 0
    assert report.beyond_by_identity["pcp"] == 0
    assert report.beyond_by_identity["gamma_pair"] == 0
    assert report.beyond_by_identity["vega_pair"] == 0


# ---------------------------------------------------------------------------
# Pair γ / vega at shared σ
# ---------------------------------------------------------------------------


def test_bsm_gamma_and_vega_are_identical_for_call_and_put() -> None:
    gv_c = drift_mod._bsm_gamma_vega(_euro_row("call"), 0.20)
    gv_p = drift_mod._bsm_gamma_vega(_euro_row("put"), 0.20)
    assert gv_c is not None and gv_p is not None
    assert gv_c[0] == pytest.approx(gv_p[0], rel=1e-12)
    assert gv_c[1] == pytest.approx(gv_p[1], rel=1e-12)


def test_vega_pair_fails_when_legs_disagree_at_shared_sigma() -> None:
    call = _euro_row("call", T=0.001)
    put = _euro_row("put")
    report = evaluate_drift(
        pa.Table.from_pylist([call, put]), thresholds=LOOSE, dt=DT, asof_ns=1, r=0.05
    )
    assert report.beyond_by_identity["vega_pair"] >= 1
    assert report.beyond_by_identity["gamma_pair"] >= 1


# ---------------------------------------------------------------------------
# Median (even length averages the two middle values)
# ---------------------------------------------------------------------------


def test_median_averages_the_two_middle_values_when_even() -> None:
    assert drift_mod._median([3.0, 1.0, 2.0]) == 2.0
    assert drift_mod._median([1.0, 2.0, 3.0, 100.0]) == 2.5
    assert drift_mod._median([1.0]) == 1.0
    assert drift_mod._median([2.0, 2.0, 2.0, 4.0]) == 2.0


# ---------------------------------------------------------------------------
# cutoff_asof_ns: 16:40 America/New_York → UTC ns (DST and standard time)
# ---------------------------------------------------------------------------


def test_cutoff_asof_ns_is_1640_et_in_edt_and_est() -> None:
    assert DEFAULT_CUTOFF_ET == "16:40"
    edt = cutoff_asof_ns(date(2026, 7, 15), "16:40")
    est = cutoff_asof_ns(date(2026, 1, 15), "16:40")
    # 16:40 EDT = 20:40 UTC; 16:40 EST = 21:40 UTC.
    assert edt == int(datetime(2026, 7, 15, 20, 40, tzinfo=UTC).timestamp() * 1e9)
    assert est == int(datetime(2026, 1, 15, 21, 40, tzinfo=UTC).timestamp() * 1e9)
    assert edt == 1_784_148_000_000_000_000
    assert est == 1_768_513_200_000_000_000
    assert datetime.fromtimestamp(edt / 1e9, tz=UTC).hour == 20
    assert datetime.fromtimestamp(est / 1e9, tz=UTC).hour == 21
