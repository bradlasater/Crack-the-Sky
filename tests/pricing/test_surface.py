"""Tests for the SVI vol surface fit on top of the ATM term structure.

The acceptance criterion is the round-trip: price a synthetic chain off a
known SVI smile, invert every OTM strike back to its own IV, refit, and the
recovered smile must match the one it was priced from. The rest pin the
arbitrage guards (butterfly repair and its loud backstop, calendar, thin
chains), the time-value pre-filter, and the term interpolation's semantics.
A real-session fit needs the box's warehouse and is the manual verification
step, not a CI test -- tests/conftest.py blocks outbound sockets and CI has
no DATA_ROOT.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest

from pricing import surface as sf
from pricing import term_structure as ts
from pricing.bsm import price

DAY = date(2026, 8, 28)
R = 0.04
F = 7700.0

NEAR = date(2026, 9, 25)   # 28 DTE
MID = date(2026, 10, 30)   # 63 DTE
FAR = date(2026, 12, 18)   # 112 DTE
T_NEAR = (NEAR - DAY).days / 365.0
T_MID = (MID - DAY).days / 365.0
T_FAR = (FAR - DAY).days / 365.0

# A plausible butterfly-clean slice: ~20% ATM vol at 28 DTE, negative skew.
PARAMS = {"a": 0.0008, "b": 0.02, "rho": -0.4, "m": 0.01, "sigma": 0.12}

STRIKES = [float(k) for k in range(7300, 8101, 25)]


def _flat_rate(_as_of, _T):  # noqa: ANN001
    return R


def _sym(root: str, expiry: date, kind: str, strike: float) -> str:
    return (f"O:{root}{expiry:%y%m%d}{'C' if kind == 'call' else 'P'}"
            f"{int(round(strike * 1000)):08d}")


def _true_w(k: float, p: dict | None = None) -> float:
    p = PARAMS if p is None else p
    d = k - p["m"]
    return p["a"] + p["b"] * (p["rho"] * d + math.hypot(d, p["sigma"]))


def _true_vol(K: float, t: float) -> float:
    return math.sqrt(_true_w(math.log(K / F)) / t)


def _chain_bars(expiry: date, t: float, vol_of_K, root: str = "SPXW",  # noqa: ANN001
                strikes=None, both_legs: bool = True) -> list[dict]:
    """Day bars for a chain priced at per-strike vols in the forward measure.

    Both legs at every strike keeps parity able to recover F; ``both_legs=False``
    writes only the OTM leg away from the forward (plus the ATM pair), which is
    all the fit should be reading.
    """
    strikes = strikes if strikes is not None else STRIKES
    bars = []
    for k in strikes:
        vol = vol_of_K(k)
        kinds = ("call", "put") if both_legs or k == F else ("put" if k < F else "call",)
        for kind in kinds:
            px = float(price(F, k, t, R, vol, kind, q=R))
            bars.append({"ticker": _sym(root, expiry, kind, k),
                         "close": px, "window_end_ns": 1})
    return bars


def _svi_bars(expiry: date = NEAR, t: float = T_NEAR) -> list[dict]:
    return _chain_bars(expiry, t, lambda k: _true_vol(k, t))


# ---------------------------------------------------------------------------
# Round-trip: the acceptance criterion
# ---------------------------------------------------------------------------

def test_round_trips_a_synthetic_svi_smile() -> None:
    surfaces = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate)
    surf = surfaces["SPXW"]
    s = surf.slices[0]
    assert s.expiration_date == NEAR.isoformat()
    # The inversion is near-exact, so the fit must sit on the true curve.
    assert s.rms_error < 1e-8
    for name in ("a", "b", "rho", "m", "sigma"):
        assert getattr(s, name) == pytest.approx(PARAMS[name], rel=0.02, abs=1e-5)
    for k in np.linspace(-0.06, 0.06, 25):
        assert s.total_variance(float(k)) == pytest.approx(_true_w(float(k)), abs=1e-8)
    for K in (7450.0, 7600.0, 7700.0, 7800.0, 7950.0):
        assert surf.vol(K, T_NEAR) == pytest.approx(_true_vol(K, T_NEAR), abs=1e-6)


def test_refit_is_deterministic() -> None:
    """The seed is data-derived: the same slice refits to the same params."""
    one = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",),
                            rate_fn=_flat_rate)["SPXW"].slices[0]
    two = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",),
                            rate_fn=_flat_rate)["SPXW"].slices[0]
    assert one == two


def test_flat_black76_smile_fits_near_flat() -> None:
    bars = _chain_bars(NEAR, T_NEAR, lambda k: 0.18)
    surf = sf.build_surfaces(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate)["SPXW"]
    s = surf.slices[0]
    assert s.b == pytest.approx(0.0, abs=1e-5)
    for K in (7400.0, 7600.0, 7700.0, 7800.0, 8000.0):
        assert surf.vol(K, T_NEAR) == pytest.approx(0.18, abs=1e-6)
    # A flat slice is arbitrage-clean: g stays positive on the padded grid
    # (with b ~ 0 the other params are non-identifiable, so the guard's
    # acceptance -- not g == 1 -- is the claim).
    assert s.min_g >= 0.0


def test_surface_agrees_with_the_atm_curve_at_the_atm_strike() -> None:
    """vol(K_atm, T) and atm_term_structure.atm_iv are two reductions of the
    same closes; they must agree to within fit error."""
    bars = _svi_bars()
    surf = sf.build_surfaces(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate)["SPXW"]
    row = ts.build_rows(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate)[0]
    assert surf.vol(row["atm_strike"], row["t_years"]) == pytest.approx(
        row["atm_iv"], abs=1e-6)


# ---------------------------------------------------------------------------
# OTM selection and root discipline
# ---------------------------------------------------------------------------

def test_fit_uses_otm_strikes_only() -> None:
    """Dropping every ITM leg must not move the fit: they were never inputs."""
    full = _svi_bars()
    otm = _chain_bars(NEAR, T_NEAR, lambda k: _true_vol(k, T_NEAR), both_legs=False)
    a = sf.build_surfaces(full, DAY, roots=("SPXW",), rate_fn=_flat_rate)["SPXW"].slices[0]
    b = sf.build_surfaces(otm, DAY, roots=("SPXW",), rate_fn=_flat_rate)["SPXW"].slices[0]
    assert a.n_strikes == len(STRIKES) - 1  # all strikes, one leg each
    assert (a.a, a.b, a.rho, a.m, a.sigma) == (b.a, b.b, b.rho, b.m, b.sigma)


def test_spy_is_refused_not_fit_under_the_wrong_model() -> None:
    """SPY is American; its strikes are not comparable under a BSM inversion."""
    bars = _chain_bars(NEAR, T_NEAR, lambda k: 0.18, root="SPY")
    with pytest.raises(sf.SurfaceError, match="European"):
        sf.build_surfaces(bars, DAY, roots=("SPY",), rate_fn=_flat_rate)


# ---------------------------------------------------------------------------
# The time-value pre-filter
# ---------------------------------------------------------------------------

def test_near_worthless_close_is_filtered_not_fit() -> None:
    """A stale far-wing close at/below the floor must not steer the fit: the
    slice fits exactly as if the print were absent."""
    garbage = {"ticker": _sym("SPXW", NEAR, "put", 7000.0),
               "close": 0.10, "window_end_ns": 1}
    assert garbage["close"] <= sf.MIN_TIME_VALUE_FRAC * F  # below the floor
    base = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate)
    dirty = sf.build_surfaces(_svi_bars() + [garbage], DAY, roots=("SPXW",),
                              rate_fn=_flat_rate)
    a = base["SPXW"].slices[0]
    b = dirty["SPXW"].slices[0]
    assert (a.n_strikes, a.a, a.b, a.rho, a.m, a.sigma) == \
           (b.n_strikes, b.a, b.b, b.rho, b.m, b.sigma)


def test_close_above_the_floor_is_fit() -> None:
    """The floor only removes near-worthless prints: a genuine wing strike
    priced off the true smile above the floor joins the fit."""
    extra_k = 7000.0
    px = float(price(F, extra_k, T_NEAR, R, _true_vol(extra_k, T_NEAR), "put", q=R))
    assert px > sf.MIN_TIME_VALUE_FRAC * F  # the fixture must clear the floor
    extra = {"ticker": _sym("SPXW", NEAR, "put", extra_k),
             "close": px, "window_end_ns": 1}
    base = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate)
    wider = sf.build_surfaces(_svi_bars() + [extra], DAY, roots=("SPXW",),
                              rate_fn=_flat_rate)
    a = base["SPXW"].slices[0]
    b = wider["SPXW"].slices[0]
    assert b.n_strikes == a.n_strikes + 1
    # ... and a point on the true curve does not move the recovered smile.
    for name in ("a", "b", "rho", "m", "sigma"):
        assert getattr(b, name) == pytest.approx(getattr(a, name), rel=0.02, abs=1e-5)


# ---------------------------------------------------------------------------
# Arbitrage guards
# ---------------------------------------------------------------------------

# Wing slope past the moment bound (b(1+|rho|) > 2) prices a negative density
# far enough into the wings; the padded guard grid is what sees it.
BUTTERFLY_BAD = {"a": 0.002, "b": 2.5, "rho": 0.0, "m": 0.0, "sigma": 0.3}


def test_butterfly_violating_smile_is_repaired_not_rejected() -> None:
    """Contract change (issue #46): the butterfly guard moved inside the fit.

    Input priced off an arbitrageable SVI smile used to raise; now the slice
    is refit under g(k) >= 0 as a hard constraint and the feasible fit is
    returned. The post-fit guard remains as the backstop for smiles the
    constrained refit cannot repair.
    """
    ks = np.linspace(-0.05, 0.05, 21)
    ws = np.array([_true_w(float(k), BUTTERFLY_BAD) for k in ks])
    # The input really is arbitrageable: the backstop still raises on it.
    with pytest.raises(sf.SurfaceArbitrageError, match="butterfly"):
        sf._check_butterfly(BUTTERFLY_BAD["a"], BUTTERFLY_BAD["b"],
                            BUTTERFLY_BAD["rho"], BUTTERFLY_BAD["m"],
                            BUTTERFLY_BAD["sigma"], float(ks[0]), float(ks[-1]))
    fit = sf.fit_slice(ks, ws)
    assert fit.min_g >= 0.0
    # Pulled back inside the moment bound, and still on the data over the
    # quoted range (the quoted smiles differ by far less than this).
    assert fit.b * (1.0 + abs(fit.rho)) <= 2.0
    assert fit.rms_error < 1e-3


def test_butterfly_violating_chain_is_repaired_end_to_end() -> None:
    bars = _chain_bars(
        NEAR, T_NEAR, lambda k: math.sqrt(_true_w(math.log(k / F), BUTTERFLY_BAD) / T_NEAR))
    surf = sf.build_surfaces(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate)["SPXW"]
    assert surf.slices[0].min_g >= 0.0


def test_repair_is_deterministic() -> None:
    """The repair path is seeded deterministically, same as the base fit."""
    ks = np.linspace(-0.05, 0.05, 21)
    ws = np.array([_true_w(float(k), BUTTERFLY_BAD) for k in ks])
    assert sf.fit_slice(ks, ws) == sf.fit_slice(ks, ws)


def test_interior_inconsistent_smile_fails_loud() -> None:
    """A smile no SVI curve can fit -- an interior notch pricing a negative
    butterfly spread at a quoted strike -- is not repairable wing noise: the
    unconstrained fit itself fails and the slice raises."""
    ks = np.linspace(-0.05, 0.05, 21)
    ws = np.array([_true_w(float(k)) for k in ks])
    ws[10] *= 0.02
    with pytest.raises(sf.SurfaceError):
        sf.fit_slice(ks, ws)


def test_svi_w_jac_matches_finite_differences() -> None:
    """The repair's analytic objective Jacobian feeds SLSQP; a wrong column
    silently converges to the wrong optimum."""
    ks = np.linspace(-0.05, 0.05, 11)
    p = np.array([0.001, 0.03, -0.4, 0.01, 0.1])
    eps = 1e-7

    def w_of(pp: np.ndarray) -> np.ndarray:
        w0, b, rho, m, sigma = (float(v) for v in pp)
        a = w0 - b * sigma * math.sqrt(1.0 - rho * rho)
        return sf._svi_w(ks, a, b, rho, m, sigma)

    fd = np.column_stack([
        (w_of(p + eps * np.eye(5)[j]) - w_of(p - eps * np.eye(5)[j])) / (2 * eps)
        for j in range(5)
    ])
    assert sf._svi_w_jac(ks, p) == pytest.approx(fd, rel=1e-5, abs=1e-10)


def test_calendar_arbitrage_is_detected() -> None:
    """The near slice richer in total variance than the far one is free money."""
    bars = (_chain_bars(NEAR, T_NEAR, lambda k: 0.30)
            + _chain_bars(FAR, T_FAR, lambda k: 0.10))
    with pytest.raises(sf.SurfaceArbitrageError, match="calendar"):
        sf.build_surfaces(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate)


def test_calendar_wing_crossing_is_repaired_not_rejected() -> None:
    """Issue #46: a slice dipping below its predecessor's wing is refit under
    w >= w_prev on the guard grid, because day-bar wings carry small genuine
    crossings. Genuine calendar arb (the test above) needs a repair too far
    from the data -- REPAIR_MAX_REL_RMS rejects it and the guard raises."""
    ks = np.linspace(-0.05, 0.05, 21)
    ws = np.array([_true_w(float(k)) for k in ks])
    base = sf.fit_slice(ks, ws)
    # A predecessor below at the centre but above in the left wing: the two
    # cross in the wing only, the real-session pattern.
    floor = sf.SliceFit(a=0.0001, b=0.035, rho=-0.4, m=0.01, sigma=0.08,
                        rms_error=0.0, min_g=1.0)
    cal_grid = np.linspace(float(ks[0]), float(ks[-1]), sf.G_POINTS)
    floor_w = sf._svi_w(cal_grid, floor.a, floor.b, floor.rho, floor.m, floor.sigma)
    base_w = sf._svi_w(cal_grid, base.a, base.b, base.rho, base.m, base.sigma)
    assert float(np.min(base_w - floor_w)) < 0.0  # the fixture must cross
    chained = sf.fit_slice(ks, ws, floor=floor, cal_grid=cal_grid)
    chained_w = sf._svi_w(cal_grid, chained.a, chained.b, chained.rho,
                          chained.m, chained.sigma)
    assert float(np.min(chained_w - floor_w)) >= -sf.CAL_TOL
    assert chained.min_g >= 0.0
    assert chained.rms_error < 1e-3  # and the repair stays on the data


# ---------------------------------------------------------------------------
# Term interpolation
# ---------------------------------------------------------------------------

def _three_expiry_surface() -> sf.Surface:
    """Flat slices at rising vols: calendar-clean, and w is known exactly."""
    bars = (_chain_bars(NEAR, T_NEAR, lambda k: 0.16)
            + _chain_bars(MID, T_MID, lambda k: 0.20)
            + _chain_bars(FAR, T_FAR, lambda k: 0.24))
    return sf.build_surfaces(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate)["SPXW"]


def test_term_interpolation_is_exact_at_fitted_expiries() -> None:
    surf = _three_expiry_surface()
    for t, s in zip((T_NEAR, T_MID, T_FAR), surf.slices, strict=True):
        assert surf.vol(7600.0, t) == s.vol(7600.0)


def test_term_interpolation_is_linear_in_total_variance() -> None:
    surf = _three_expiry_surface()
    t_mid = (T_NEAR + T_MID) / 2
    w = 0.5 * (0.16 * 0.16 * T_NEAR + 0.20 * 0.20 * T_MID)
    assert surf.vol(7600.0, t_mid) == pytest.approx(math.sqrt(w / t_mid), rel=1e-9)


def test_term_interpolation_is_continuous_across_expiries() -> None:
    surf = _three_expiry_surface()
    eps = 1e-9
    assert surf.vol(7600.0, T_MID + eps) == pytest.approx(
        surf.vol(7600.0, T_MID - eps), abs=1e-7)


def test_vol_is_flat_outside_the_fitted_term_range() -> None:
    surf = _three_expiry_surface()
    assert surf.vol(7600.0, 0.5 * T_NEAR) == surf.vol(7600.0, T_NEAR)
    assert surf.vol(7600.0, 2.0 * T_FAR) == surf.vol(7600.0, T_FAR)


# ---------------------------------------------------------------------------
# Thin chains fail loud at the fit, quietly at the build
# ---------------------------------------------------------------------------

def test_too_few_strikes_fails_loud() -> None:
    ks = np.linspace(-0.03, 0.03, 4)
    ws = np.full(4, 0.18 * 0.18 * T_NEAR)
    with pytest.raises(sf.SurfaceError, match="at least"):
        sf.fit_slice(ks, ws)


def test_thin_expiry_is_skipped_not_fit() -> None:
    """Day bars hold only contracts that traded; a sparse expiry is no smile."""
    # Three or four OTM points (the strike on the forward flips side with the
    # parity print's last ulp) -- under MIN_STRIKES either way.
    strikes = [7640.0, 7670.0, 7700.0, 7730.0]
    bars = _chain_bars(NEAR, T_NEAR, lambda k: 0.18, strikes=strikes)
    assert sf.build_surfaces(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate) == {}


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------

def test_rows_match_the_landed_schema() -> None:
    from ingest import schemas

    surfaces = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate)
    rows = sf.rows_from_surfaces(surfaces)
    fields = {f.name for f in schemas.SCHEMAS[sf.DATASET]}
    assert set(rows[0]) == fields, "extra keys are dropped silently on write"
