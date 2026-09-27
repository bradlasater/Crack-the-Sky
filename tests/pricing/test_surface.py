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
from pricing.daycount import ACT_365

# An arbitrary calendar-grid size for tests that build one by hand.
CAL_POINTS = 201

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
    surfaces = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)
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
                            rate_fn=_flat_rate, daycount=ACT_365)["SPXW"].slices[0]
    two = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",),
                            rate_fn=_flat_rate, daycount=ACT_365)["SPXW"].slices[0]
    assert one == two


def test_flat_black76_smile_fits_near_flat() -> None:
    bars = _chain_bars(NEAR, T_NEAR, lambda k: 0.18)
    surf = sf.build_surfaces(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
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
    surf = sf.build_surfaces(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
    row = ts.build_rows(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)[0]
    assert surf.vol(row["atm_strike"], row["t_years"]) == pytest.approx(
        row["atm_iv"], abs=1e-6)


# ---------------------------------------------------------------------------
# OTM selection and root discipline
# ---------------------------------------------------------------------------

def test_fit_uses_otm_strikes_only() -> None:
    """Dropping every ITM leg must not move the fit: they were never inputs."""
    full = _svi_bars()
    otm = _chain_bars(NEAR, T_NEAR, lambda k: _true_vol(k, T_NEAR), both_legs=False)
    a = sf.build_surfaces(full, DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)["SPXW"].slices[0]
    b = sf.build_surfaces(otm, DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)["SPXW"].slices[0]
    assert a.n_strikes == len(STRIKES) - 1  # all strikes, one leg each
    assert (a.a, a.b, a.rho, a.m, a.sigma) == (b.a, b.b, b.rho, b.m, b.sigma)


def test_spy_is_refused_not_fit_under_the_wrong_model() -> None:
    """SPY is American; its strikes are not comparable under a BSM inversion."""
    bars = _chain_bars(NEAR, T_NEAR, lambda k: 0.18, root="SPY")
    with pytest.raises(sf.SurfaceError, match="European"):
        sf.build_surfaces(bars, DAY, roots=("SPY",), rate_fn=_flat_rate, daycount=ACT_365)


# ---------------------------------------------------------------------------
# The time-value pre-filter
# ---------------------------------------------------------------------------

def test_near_worthless_close_is_filtered_not_fit() -> None:
    """A stale far-wing close at/below the floor must not steer the fit: the
    slice fits exactly as if the print were absent."""
    garbage = {"ticker": _sym("SPXW", NEAR, "put", 7000.0),
               "close": 0.10, "window_end_ns": 1}
    assert garbage["close"] <= sf.MIN_TIME_VALUE_FRAC * F  # below the floor
    base = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)
    dirty = sf.build_surfaces(_svi_bars() + [garbage], DAY, roots=("SPXW",),
                              rate_fn=_flat_rate, daycount=ACT_365)
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
    base = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)
    wider = sf.build_surfaces(_svi_bars() + [extra], DAY, roots=("SPXW",),
                              rate_fn=_flat_rate, daycount=ACT_365)
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


# The butterfly check reaches three of the slice's standard deviations past
# its strikes, not a flat 1.0 in k (fix F).

# A real SPXW slice: 2022-09-02's 7-day expiry, strikes k in [-0.270, 0.069].
# Its unconstrained fit is arbitrage-free there and for several standard
# deviations beyond, and fails only far out in the extrapolated wing.
SHORT_REAL = {"a": -0.00211003, "b": 1.72961, "rho": -0.999, "m": -0.2991, "sigma": 0.0293}
SHORT_REAL_STRIKES = (-0.270, 0.069)


def test_butterfly_pad_scales_with_the_slice_and_is_capped() -> None:
    short = [0.0002, 0.0004, 0.0009]      # a few days: sqrt(min w) ~ 0.014
    long_ = [0.09, 0.11, 0.2]             # two years at ~20% vol
    assert sf._butterfly_pad(short) == pytest.approx(3 * math.sqrt(0.0002))
    assert sf._butterfly_pad(long_) == pytest.approx(3 * math.sqrt(0.09))
    assert sf._butterfly_pad([0.5, 0.8]) == sf.G_PAD  # never further than before


def test_arbitrage_only_far_past_a_short_slice_is_not_rejected() -> None:
    p, (lo, hi) = SHORT_REAL, SHORT_REAL_STRIKES
    with pytest.raises(sf.SurfaceArbitrageError, match="butterfly"):
        sf._check_butterfly(*p.values(), lo, hi)  # the old flat pad
    w_min = p["a"] + p["b"] * p["sigma"] * math.sqrt(1 - p["rho"] ** 2)
    pad = sf._butterfly_pad([w_min])
    assert pad < 0.1
    assert sf._check_butterfly(*p.values(), lo, hi, pad) >= 0.0


# The butterfly check is exact over its range, not only at grid points (fix H).

# 2022-11-29's SPXW 2022-12-07 expiry as a fixed 201-point grid let it land:
# negative density in a band ~0.01 wide at k ~ -0.176 to -0.166, straddling
# the last quoted put, between two of that grid's points ~0.011 apart.
STEPPED_OVER = {"a": 0.000397112, "b": 0.0209383, "rho": -0.999, "m": -0.0909669, "sigma": 0.0586978}
STEPPED_OVER_STRIKES = (-0.1667022, 0.0594220)

# 2023-08-08's SPXW 1-day expiry, as landed: a near-tangent dip to
# g ~ -2.9e-6 inside its strikes that even a 0.001 lattice misses.
TANGENT_DIP = {"a": -0.000269700, "b": 0.0105933, "rho": -0.668726, "m": -0.0237484, "sigma": 0.0380497}
TANGENT_DIP_STRIKES = (-0.0748541, 0.0144500)


def test_butterfly_grid_is_the_lattice_plus_its_ends() -> None:
    grid = sf._butterfly_grid(-0.1234, 0.0567, pad=0.05)
    assert grid[0] == pytest.approx(-0.1734) and grid[-1] == pytest.approx(0.1067)
    assert float(np.max(np.diff(grid))) <= sf.K_STEP + 1e-12
    assert np.allclose(grid[1:-1] / sf.K_STEP, np.round(grid[1:-1] / sf.K_STEP))


def test_a_band_between_the_old_grid_points_is_rejected() -> None:
    p, (lo, hi) = STEPPED_OVER, STEPPED_OVER_STRIKES
    old_grid = np.linspace(lo - sf.G_PAD, hi + sf.G_PAD, 201)
    assert float(np.min(sf._g(old_grid, *p.values()))) >= 0.0  # what let it land
    with pytest.raises(sf.SurfaceArbitrageError, match="butterfly"):
        sf._check_butterfly(*p.values(), lo, hi, sf.G_PAD)


def test_a_dip_narrower_than_any_lattice_is_rejected() -> None:
    p, (lo, hi) = TANGENT_DIP, TANGENT_DIP_STRIKES
    fine = np.arange(math.ceil(lo / 0.001), math.floor(hi / 0.001) + 1) * 0.001
    assert float(np.min(sf._g(fine, *p.values()))) >= 0.0
    # The guard's own grid points miss it too; the refinement between them
    # is what finds it.
    grid = sf._butterfly_grid(lo, hi, pad=0.0)
    assert float(np.min(sf._g(grid, *p.values()))) >= 0.0
    with pytest.raises(sf.SurfaceArbitrageError, match="butterfly"):
        sf._check_butterfly(*p.values(), lo, hi, pad=0.0)


def test_the_refinement_agrees_with_a_dense_grid_on_a_clean_smile() -> None:
    a, b, rho, m, sigma = 0.02, 0.1, -0.4, 0.01, 0.12
    grid = sf._butterfly_grid(-0.3, 0.3, pad=0.1)
    dense = np.linspace(grid[0], grid[-1], 200001)
    exact = float(np.min(sf._g(dense, a, b, rho, m, sigma)))
    assert sf._min_g_svi(grid, a, b, rho, m, sigma) == pytest.approx(exact, abs=1e-9)


def test_arbitrage_within_the_strikes_is_still_rejected() -> None:
    """Narrowing the pad changes nothing inside the data."""
    with pytest.raises(sf.SurfaceArbitrageError, match=r"strikes \[-0.600, 0.600\]"):
        sf._check_butterfly(*BUTTERFLY_BAD.values(), -0.6, 0.6, pad=0.01)


def test_butterfly_violating_chain_is_repaired_end_to_end() -> None:
    bars = _chain_bars(
        NEAR, T_NEAR, lambda k: math.sqrt(_true_w(math.log(k / F), BUTTERFLY_BAD) / T_NEAR))
    surf = sf.build_surfaces(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
    assert surf.slices[0].min_g >= 0.0


def test_repair_is_deterministic() -> None:
    """The repair path is seeded deterministically, same as the base fit."""
    ks = np.linspace(-0.05, 0.05, 21)
    ws = np.array([_true_w(float(k), BUTTERFLY_BAD) for k in ks])
    assert sf.fit_slice(ks, ws) == sf.fit_slice(ks, ws)


# A smile whose center sits right of the quoted window: over the window the
# curve is its left-wing asymptote, so the unconstrained optimum recovers the
# center outside the data. This is the 2026-09-28 SPXW basin (m=+0.82 with
# the last quote at k=0.057) that priced negative density in the wing.
CENTER_BEYOND_QUOTES = {"a": 0.0002, "b": 0.3, "rho": 0.6, "m": 0.4, "sigma": 0.25}


def test_m_cannot_escape_the_quoted_strikes() -> None:
    """The m bound is what closes the spurious basin: data whose unconstrained
    optimum parks the smile's center past the last quote must come back with
    m inside the quoted range, still arbitrage-clean. Against the old [-3, 3]
    m bounds this fit returns m ~= 0.39, so the test pins the regression."""
    ks = np.linspace(-0.12, 0.06, 40)
    ws = np.array([_true_w(float(k), CENTER_BEYOND_QUOTES) for k in ks])
    # The premise: the generating center is genuinely outside the window, so
    # the unconstrained optimum has somewhere to escape to.
    assert CENTER_BEYOND_QUOTES["m"] > ks[-1]
    fit = sf.fit_slice(ks, ws)
    assert ks[0] <= fit.m <= ks[-1]
    assert fit.min_g >= 0.0


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
    """The near slice richer in total variance than the far one is free money.

    The Surface guard rejects the pair; the build, fitting in expiry order,
    drops the far slice that cannot clear the near one's floor (fix C).
    """
    bars = (_chain_bars(NEAR, T_NEAR, lambda k: 0.30)
            + _chain_bars(FAR, T_FAR, lambda k: 0.10))
    near = sf.build_surfaces(_chain_bars(NEAR, T_NEAR, lambda k: 0.30), DAY, roots=("SPXW",),
                             rate_fn=_flat_rate, daycount=ACT_365)["SPXW"].slices[0]
    far = sf.build_surfaces(_chain_bars(FAR, T_FAR, lambda k: 0.10), DAY, roots=("SPXW",),
                            rate_fn=_flat_rate, daycount=ACT_365)["SPXW"].slices[0]
    with pytest.raises(sf.SurfaceArbitrageError, match="calendar"):
        sf.Surface(DAY, "SPXW", [near, far])

    surf = sf.build_surfaces(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
    assert [s.expiration_date for s in surf.slices] == [NEAR.isoformat()]
    [dropped] = surf.dropped
    assert dropped.expiration_date == FAR.isoformat()
    assert dropped.dte == (FAR - DAY).days
    assert dropped.reason.startswith("SurfaceArbitrageError: calendar arbitrage")


# A slice that cannot be fit is dropped, not the root (fix C).

def _notched(expiry: date, t: float) -> list[dict]:
    """A chain with one quoted strike's vol halved: no SVI curve converges on it."""
    return _chain_bars(expiry, t, lambda K: _true_vol(K, t) * (0.5 if K == 7725 else 1.0))


def _clean(expiry: date, t: float) -> list[dict]:
    return _chain_bars(expiry, t, lambda K: _true_vol(K, t))


def test_a_slice_that_cannot_be_fit_is_dropped_not_the_root() -> None:
    bars = _clean(NEAR, T_NEAR) + _notched(MID, T_MID) + _clean(FAR, T_FAR)
    surf = sf.build_surfaces(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
    assert [s.expiration_date for s in surf.slices] == [NEAR.isoformat(), FAR.isoformat()]
    [dropped] = surf.dropped
    assert (dropped.expiration_date, dropped.dte) == (MID.isoformat(), (MID - DAY).days)
    assert dropped.reason.startswith("SurfaceError: SVI fit did not converge")
    # The chain continues from the kept slices: the far slice is floored by
    # the near one alone, exactly as if the middle expiry had never quoted.
    without = sf.build_surfaces(_clean(NEAR, T_NEAR) + _clean(FAR, T_FAR), DAY, roots=("SPXW",),
                                rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
    assert surf.slices == without.slices
    assert without.dropped == ()


def test_a_root_that_keeps_no_slice_still_raises() -> None:
    with pytest.raises(sf.SurfaceError, match=r"every SPXW slice on 2026-08-28 was dropped: 2026-10-30 "):
        sf.build_surfaces(_notched(MID, T_MID), DAY, roots=("SPXW",),
                          rate_fn=_flat_rate, daycount=ACT_365)


def test_a_surface_read_back_has_no_dropped_slices() -> None:
    surf = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate,
                             daycount=ACT_365)["SPXW"]
    assert sf.Surface.from_rows(sf.rows_from_surfaces({"SPXW": surf})).dropped == ()


# Calendar checks compare two slices only where both have data (fix B).

def _ranged(expiry: str, sessions: int, k_min: float, k_max: float, **params: float) -> sf.Slice:
    """A slice from known SVI parameters, fit (notionally) to ``[k_min, k_max]``."""
    p = {**PARAMS, **params}
    return sf.Slice(
        expiration_date=expiry, dte=sessions, t_years=sessions / 252, forward=7700.0,
        a=p["a"], b=p["b"], rho=p["rho"], m=p["m"], sigma=p["sigma"],
        k_min=k_min, k_max=k_max, n_strikes=21, rms_error=0.0, min_g=0.5,
        rate=R, daycount="bus/252",
    )


def test_a_crossing_outside_the_overlap_is_not_arbitrage() -> None:
    """A short slice's steep left wing crosses a longer, flatter one far past
    both slices' strikes. Nothing was priced there, so nothing is rejected."""
    near = _ranged("2026-09-02", 3, -0.10, 0.05, a=0.0002, b=0.004, rho=-0.9, m=0.0, sigma=0.02)
    far = _ranged("2026-09-11", 10, -0.10, 0.05, a=0.0012, b=0.003, rho=0.0, m=0.0, sigma=0.05)
    inside = np.linspace(-0.10, 0.05, 51)
    assert all(far.total_variance(k) > near.total_variance(k) for k in inside)
    k = -1.0  # far outside [-0.10, 0.05]
    assert near.total_variance(k) > far.total_variance(k)  # the wing crossing is real
    assert len(sf.Surface(DAY, "SPXW", [near, far])) == 2


def test_a_crossing_inside_the_overlap_is_still_rejected() -> None:
    near = _ranged("2026-09-02", 3, -0.10, 0.05, a=0.0030)
    far = _ranged("2026-09-11", 10, -0.10, 0.05, a=0.0008)
    with pytest.raises(sf.SurfaceArbitrageError, match="calendar arbitrage"):
        sf.Surface(DAY, "SPXW", [near, far])


def test_disjoint_slices_have_nothing_to_compare() -> None:
    near = _ranged("2026-09-02", 3, -0.10, -0.05, a=0.0030)
    far = _ranged("2026-09-11", 10, 0.00, 0.05, a=0.0008)
    assert len(sf.Surface(DAY, "SPXW", [near, far])) == 2


def test_non_adjacent_slices_are_compared_where_they_overlap() -> None:
    """The middle slice covers none of the region where the outer two cross,
    so an adjacent-only check would pass them."""
    first = _ranged("2026-09-02", 3, -0.30, -0.20, a=0.0030)
    middle = _ranged("2026-09-04", 5, 0.00, 0.05, a=0.0040)
    last = _ranged("2026-09-11", 10, -0.30, -0.20, a=0.0008)
    with pytest.raises(sf.SurfaceArbitrageError, match="2026-09-02 .* and 2026-09-11"):
        sf.Surface(DAY, "SPXW", [first, middle, last])


def test_floor_and_guard_share_one_lattice() -> None:
    """The fit constrains and the guard checks the same points, bit for bit;
    two different samplings found hairline crossings between each other's
    points on 1,106 landed root-surfaces."""
    a = sf._lattice(-0.1234, 0.0567)
    b = sf._lattice(-0.3, 0.2)
    shared = np.intersect1d(a, b)
    assert shared.size == a.size  # every point of the narrower range is in the wider
    assert np.all(np.round(a / sf.K_STEP) * sf.K_STEP == a)


def test_calendar_floor_is_the_highest_earlier_slice_where_it_has_data() -> None:
    low = _ranged("2026-09-02", 3, -0.10, 0.00, a=0.0010)
    high = _ranged("2026-09-04", 5, -0.05, 0.10, a=0.0020)
    grid, floor = sf._calendar_floor(-0.20, 0.20, [low, high])
    assert grid.min() >= -0.10 and grid.max() <= 0.10  # uncovered points dropped
    both = (grid >= -0.05) & (grid <= 0.00)
    assert np.allclose(floor[both], [high.total_variance(k) for k in grid[both]])
    only_low = grid < -0.05
    assert np.allclose(floor[only_low], [low.total_variance(k) for k in grid[only_low]])
    assert sf._calendar_floor(0.5, 0.6, [low, high]) is None


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
    cal_grid = np.linspace(float(ks[0]), float(ks[-1]), CAL_POINTS)
    floor_w = sf._svi_w(cal_grid, floor.a, floor.b, floor.rho, floor.m, floor.sigma)
    base_w = sf._svi_w(cal_grid, base.a, base.b, base.rho, base.m, base.sigma)
    assert float(np.min(base_w - floor_w)) < 0.0  # the fixture must cross
    chained = sf.fit_slice(ks, ws, floor=floor, cal_grid=cal_grid)
    chained_w = sf._svi_w(cal_grid, chained.a, chained.b, chained.rho,
                          chained.m, chained.sigma)
    assert float(np.min(chained_w - floor_w)) >= -sf.CAL_TOL
    assert chained.min_g >= 0.0
    assert chained.rms_error < 1e-3  # and the repair stays on the data


def test_build_surfaces_chains_the_calendar_repair() -> None:
    """Copilot review on #49: cover the two-pass build wiring itself.

    A later slice whose unconstrained fit dips below the earlier slice must
    be repaired inside build_surfaces -- forwarding the earlier slices' floor
    on the shared lattice, and the expiry ordering -- not just inside
    fit_slice. Clean means clean at the lattice points where both slices
    have data: that is what the fit constrains and the Surface guard checks.
    """
    delta = 2e-4
    lower_smile = {**PARAMS, "a": PARAMS["a"] - delta}
    bars = (_svi_bars()  # NEAR off the true smile
            + _chain_bars(FAR, T_FAR, lambda k: math.sqrt(
                _true_w(math.log(k / F), lower_smile) / T_FAR)))
    surf = sf.build_surfaces(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
    assert len(surf.slices) == 2
    near, far = surf.slices
    grid = sf._lattice(*sf._overlap(near.k_min, near.k_max, far.k_min, far.k_max))
    assert grid.size > 10
    near_w = sf._svi_w(grid, near.a, near.b, near.rho, near.m, near.sigma)
    far_w = sf._svi_w(grid, far.a, far.b, far.rho, far.m, far.sigma)
    assert float(np.min(far_w - near_w)) >= -sf.CAL_TOL  # calendar-clean
    assert far.min_g >= 0.0

    # The fixture really exercises the repair: the far slice's unconstrained
    # fit on the same points does cross the near slice...
    chain = sf.bars_to_chain(bars, "SPXW")
    legs = ts._legs_by_expiry(chain)
    fwd = next(f for f in sf.forward_from_parity(chain, lambda e: R, asof_date=DAY)
               if f["expiration_date"].startswith(FAR.isoformat()))
    ks, ws = sf._expiry_points(float(fwd["forward"]), T_FAR, R,
                               legs[fwd["expiration_date"]])
    solo = sf.fit_slice(ks, ws)  # no floor: the unchained fit
    solo_w = sf._svi_w(grid, solo.a, solo.b, solo.rho, solo.m, solo.sigma)
    assert float(np.min(solo_w - near_w)) < -sf.CAL_TOL
    # ... and the repair stayed near the data rather than fabricating.
    assert far.rms_error < sf.REPAIR_MAX_REL_RMS * float(np.mean(np.asarray(ws)))


def test_flat_fallback_anchor_clears_the_floor_by_the_margin() -> None:
    """Copilot review on #49: the flat anchor used to sit at exactly
    max(w_floor), where the calendar difference is 0 -- infeasible by the
    margin both the projection scan and the SLSQP constraint require, handing
    SLSQP the infeasible start the projection exists to avoid."""
    ks = np.linspace(-0.05, 0.05, 21)
    ws = np.array([_true_w(float(k)) for k in ks])
    grid = sf._butterfly_grid(float(ks[0]), float(ks[-1]))
    cal_grid = np.linspace(float(ks[0]), float(ks[-1]), CAL_POINTS)
    # A floor far above the data: every segment point short of the flat
    # anchor is infeasible, so the anchor itself is what must qualify.
    w_floor = np.full(CAL_POINTS, 0.5)
    lower = np.array([0.0, 0.0, -0.999, -3.0, 1e-4])
    upper = np.array([10.0, 10.0, 0.999, 3.0, 5.0])
    p0 = np.array([max(float(ws.min()), 1e-8), 0.02, -0.4, 0.01, 0.12])
    p = sf._project_to_feasible(p0, ws, grid, lower, upper, w_floor, cal_grid)
    w0, b, rho, m, sigma = (float(v) for v in p)
    a = w0 - b * sigma * math.sqrt(1.0 - rho * rho)
    diff = sf._svi_w(cal_grid, a, b, rho, m, sigma) - w_floor
    assert float(np.min(diff)) >= sf.G_REPAIR_MARGIN
    assert sf._min_g(grid, p) >= sf.G_REPAIR_MARGIN


def test_flat_fallback_raises_when_no_feasible_seed_exists() -> None:
    """A calendar floor peaking past the w0 box bound leaves no feasible flat
    seed; the projection must fail loudly, not return an infeasible start."""
    ks = np.linspace(-0.05, 0.05, 21)
    ws = np.array([_true_w(float(k)) for k in ks])
    grid = sf._butterfly_grid(float(ks[0]), float(ks[-1]))
    cal_grid = np.linspace(float(ks[0]), float(ks[-1]), CAL_POINTS)
    w_floor = np.full(CAL_POINTS, 20.0)  # past the w0 box bound (10.0)
    lower = np.array([0.0, 0.0, -0.999, -3.0, 1e-4])
    upper = np.array([10.0, 10.0, 0.999, 3.0, 5.0])
    p0 = np.array([max(float(ws.min()), 1e-8), 0.02, -0.4, 0.01, 0.12])
    with pytest.raises(sf.SurfaceError, match="no feasible seed"):
        sf._project_to_feasible(p0, ws, grid, lower, upper, w_floor, cal_grid)


# ---------------------------------------------------------------------------
# Term interpolation
# ---------------------------------------------------------------------------

def _three_expiry_surface() -> sf.Surface:
    """Flat slices at rising vols: calendar-clean, and w is known exactly.

    Built from explicit flat Slices rather than a fit of synthetic flat
    chains: the interpolation contract asserted below is exact, and perfectly
    flat data is the SVI parameterisation's most degenerate case -- which of
    the many equivalent near-flat optima the solver stops at depends on the
    parameter bounds (fitting flat data once relied on a free ``m``), which
    is the fitter's business, not interpolation's.
    """
    def flat_slice(expiry: date, t: float, vol: float) -> sf.Slice:
        return sf.Slice(
            expiration_date=expiry.isoformat(), dte=(expiry - DAY).days,
            t_years=t, forward=F, a=vol * vol * t, b=0.0, rho=0.0, m=0.0,
            sigma=0.1, k_min=math.log(STRIKES[0] / F), k_max=math.log(STRIKES[-1] / F),
            n_strikes=len(STRIKES), rms_error=0.0, min_g=1.0, rate=R,
            daycount="act/365",
        )
    return sf.Surface(DAY, "SPXW", [
        flat_slice(NEAR, T_NEAR, 0.16),
        flat_slice(MID, T_MID, 0.20),
        flat_slice(FAR, T_FAR, 0.24),
    ])


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
    assert sf.build_surfaces(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365) == {}


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------

def test_rows_match_the_landed_schema() -> None:
    from ingest import schemas

    surfaces = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)
    rows = sf.rows_from_surfaces(surfaces)
    fields = {f.name for f in schemas.SCHEMAS[sf.DATASET]}
    assert set(rows[0]) == fields, "extra keys are dropped silently on write"


# ---------------------------------------------------------------------------
# Read-back: landed parameters must reconstruct the same Surface
# ---------------------------------------------------------------------------


def test_from_rows_round_trips_a_fitted_surface() -> None:
    surf = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
    loaded = sf.Surface.from_rows(sf.rows_from_surfaces({"SPXW": surf}))
    assert loaded.date == surf.date
    assert loaded.underlying == "SPXW"
    assert loaded.slices == surf.slices
    assert loaded.vol(7700.0, T_NEAR) == pytest.approx(surf.vol(7700.0, T_NEAR))


def test_from_rows_still_calendar_checks() -> None:
    """A tampered parquet must not evaluate as a smile."""
    near = {
        "date": DAY.isoformat(), "underlying": "SPXW",
        "expiration_date": NEAR.isoformat(), "dte": (NEAR - DAY).days,
        "t_years": T_NEAR, "daycount": "act/365", "forward": F,
        "svi_a": 0.04, "svi_b": 0.01, "svi_rho": 0.0, "svi_m": 0.0, "svi_sigma": 0.1,
        "k_min": -0.05, "k_max": 0.05, "n_strikes": 21,
        "rms_error": 0.0, "min_g": 0.5, "rate": R, "src": "day_bars",
        "blas_threads": 1,
    }
    far = dict(near, expiration_date=FAR.isoformat(),
               dte=(FAR - DAY).days, t_years=T_FAR, svi_a=0.001)
    with pytest.raises(sf.SurfaceArbitrageError, match="calendar"):
        sf.Surface.from_rows([near, far])


def test_from_rows_refuses_mixed_roots_or_dates() -> None:
    surf = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
    rows = sf.rows_from_surfaces({"SPXW": surf})
    mixed = [dict(rows[0], underlying="SPX")]
    with pytest.raises(sf.SurfaceError, match="one \\(date, underlying\\)"):
        sf.Surface.from_rows(rows + mixed)
    with pytest.raises(sf.SurfaceError, match="one \\(date, underlying\\)"):
        sf.Surface.from_rows([dict(rows[0], date="2020-01-02")] + rows)


def test_from_rows_refuses_an_american_root() -> None:
    surf = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
    rows = [dict(sf.rows_from_surfaces({"SPXW": surf})[0], underlying="SPY")]
    with pytest.raises(sf.SurfaceError, match="European index roots"):
        sf.Surface.from_rows(rows)


# ---------------------------------------------------------------------------
# One root's failure must not discard another (surface-coverage step 2)
# ---------------------------------------------------------------------------

class _FlatCurve:
    def at(self, _T: float) -> float:
        return R


def _session_inputs(monkeypatch, fail: tuple[str, ...]) -> None:  # noqa: ANN001
    """Two clean chains, with the named roots forced to raise in the fit."""
    bars = _svi_bars() + _chain_bars(NEAR, T_NEAR, lambda k: _true_vol(k, T_NEAR), root="SPX")
    monkeypatch.setattr(sf, "read_day_bars", lambda _s, _d: bars)
    monkeypatch.setattr(sf, "load_curve", lambda _d, _root=None: _FlatCurve())
    real = sf.build_surfaces

    def build(bars, d, roots, *args, **kwargs):  # noqa: ANN001, ANN202
        if set(roots) & set(fail):
            raise sf.SurfaceArbitrageError(f"calendar arbitrage in {roots}")
        return real(bars, d, roots, *args, **kwargs)

    monkeypatch.setattr(sf, "build_surfaces", build)


def _settings(tmp_path):  # noqa: ANN001, ANN202
    from ingest.common.config import Settings

    return Settings(massive_api_key="k", data_root=tmp_path, log_root=tmp_path / "logs")


def test_session_lands_spxw_when_spx_fails(tmp_path, monkeypatch) -> None:
    """184 of the 347 missing sessions were exactly this: SPXW fit, SPX did
    not, and the exception discarded both."""
    _session_inputs(monkeypatch, fail=("SPX",))
    got = sf.build_session(_settings(tmp_path), DAY, daycount=ACT_365)
    assert set(got.surfaces) == {"SPXW"}
    assert set(got.failures) == {"SPX"}
    assert got.failures["SPX"].startswith("SurfaceArbitrageError: ")
    # The root that landed is the same fit it would be on its own.
    alone = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate,
                              daycount=ACT_365)["SPXW"]
    assert got.surfaces["SPXW"].slices == alone.slices


def test_session_reports_every_root_when_none_fits(tmp_path, monkeypatch) -> None:
    _session_inputs(monkeypatch, fail=("SPX", "SPXW"))
    got = sf.build_session(_settings(tmp_path), DAY, daycount=ACT_365)
    assert got.surfaces == {}
    assert set(got.failures) == {"SPX", "SPXW"}


def test_build_for_date_stays_strict(tmp_path, monkeypatch) -> None:
    """The diagnostic relies on the exception itself, so the strict path stays."""
    _session_inputs(monkeypatch, fail=("SPX",))
    with pytest.raises(sf.SurfaceArbitrageError):
        sf.build_for_date(_settings(tmp_path), DAY, daycount=ACT_365)


def _job_args(**kw):  # noqa: ANN003, ANN202
    import argparse

    return argparse.Namespace(**{"date": DAY.isoformat(), "underlying": None,
                                 "dry_run": False, **kw})


def test_job_writes_the_root_that_fit_and_logs_the_other(tmp_path, monkeypatch) -> None:
    from ingest.common.logging_utils import JsonlLogger

    _session_inputs(monkeypatch, fail=("SPX",))
    monkeypatch.setattr(sf, "hybrid_for", lambda _root=None: ACT_365)
    logger = JsonlLogger(path=None, echo=False)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(logger, "log", lambda e, **f: events.append((e, f)) or {})
    summary = sf._main_fn(_job_args(), _settings(tmp_path), logger)
    assert summary["roots"] == 1 and summary["roots_failed"] == 1
    assert sf.load_surface(_settings(tmp_path), DAY, "SPXW")
    with pytest.raises(sf.SurfaceError, match="no vol_surface rows for SPX"):
        sf.load_surface(_settings(tmp_path), DAY, "SPX")
    failed = [f for e, f in events if e == "surface_root_failed"]
    assert [f["root"] for f in failed] == ["SPX"]


def test_job_logs_each_dropped_slice(tmp_path, monkeypatch) -> None:
    """coverage_audit reads these events back (decision 3)."""
    from ingest.common.logging_utils import JsonlLogger

    bars = _clean(NEAR, T_NEAR) + _notched(MID, T_MID)
    monkeypatch.setattr(sf, "read_day_bars", lambda _s, _d: bars)
    monkeypatch.setattr(sf, "load_curve", lambda _d, _root=None: _FlatCurve())
    monkeypatch.setattr(sf, "hybrid_for", lambda _root=None: ACT_365)
    logger = JsonlLogger(path=None, echo=False)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(logger, "log", lambda e, **f: events.append((e, f)) or {})
    summary = sf._main_fn(_job_args(underlying="SPXW"), _settings(tmp_path), logger)
    assert summary["slices_dropped"] == 1
    [drop] = [f for e, f in events if e == "surface_slice_dropped"]
    assert (drop["root"], drop["expiry"], drop["dte"]) == ("SPXW", MID.isoformat(), (MID - DAY).days)
    assert drop["reason"].startswith("SurfaceError: ")
    assert [s.expiration_date for s in sf.load_surface(_settings(tmp_path), DAY, "SPXW").slices] \
        == [NEAR.isoformat()]


def test_job_fails_naming_each_root_when_none_fits(tmp_path, monkeypatch) -> None:
    from ingest.common.logging_utils import JsonlLogger

    _session_inputs(monkeypatch, fail=("SPX", "SPXW"))
    monkeypatch.setattr(sf, "hybrid_for", lambda _root=None: ACT_365)
    with pytest.raises(sf.SurfaceError, match=r"SPX: .*SPXW: "):
        sf._main_fn(_job_args(), _settings(tmp_path), JsonlLogger(path=None, echo=False))


def test_load_surface_reads_what_write_rows_landed(tmp_path) -> None:
    from ingest.common.config import Settings

    surfaces = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)
    settings = Settings(massive_api_key="k", data_root=tmp_path, log_root=tmp_path / "logs")
    sf.write_rows(settings, DAY, sf.rows_from_surfaces(surfaces))
    loaded = sf.load_surface(settings, DAY, "SPXW")
    assert loaded.slices == surfaces["SPXW"].slices
    with pytest.raises(sf.SurfaceError, match="no vol_surface rows for SPX"):
        sf.load_surface(settings, DAY, "SPX")
    with pytest.raises(sf.SurfaceError, match="no vol_surface partition"):
        sf.load_surface(settings, date(2020, 1, 2), "SPXW")


def test_load_surface_refuses_a_mismatched_row_date(tmp_path) -> None:
    """A dt= partition whose rows are dated some other session must not answer as T."""
    from ingest.common.config import Settings

    surfaces = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)
    settings = Settings(massive_api_key="k", data_root=tmp_path, log_root=tmp_path / "logs")
    rows = [dict(r, date="2020-01-02") for r in sf.rows_from_surfaces(surfaces)]
    sf.write_rows(settings, DAY, rows)
    with pytest.raises(sf.SurfaceError, match="have date="):
        sf.load_surface(settings, DAY, "SPXW")


def test_from_rows_refuses_a_missing_or_unknown_daycount_stamp() -> None:
    """Unstamped T would be interpreted under whichever convention the reader
    assumed -- the silent mixing the stamp exists to prevent."""
    surf = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",),
                             rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
    rows = sf.rows_from_surfaces({"SPXW": surf})
    missing = [dict(rows[0], daycount=None)]
    with pytest.raises(sf.SurfaceError, match="no daycount stamp"):
        sf.Surface.from_rows(missing)
    unknown = [dict(rows[0], daycount="hybrid")]
    with pytest.raises(sf.SurfaceError, match="unknown daycount"):
        sf.Surface.from_rows(unknown)


def test_from_rows_accepts_the_hybrid_mix_of_stamps() -> None:
    """LEAPS fall back to ACT/365 while the rest of the book is bus/252.
    Mixing those stamps in one surface is the hybrid's normal shape."""
    surf = _three_expiry_surface()
    rows = sf.rows_from_surfaces({"SPXW": surf})
    assert len(rows) >= 2
    rows[0]["daycount"] = "bus/252"
    rows[-1]["daycount"] = "act/365"
    loaded = sf.Surface.from_rows(rows)
    assert [s.daycount for s in loaded.slices][0] == "bus/252"
    assert [s.daycount for s in loaded.slices][-1] == "act/365"


# ---------------------------------------------------------------------------
# BLAS thread-pin stamp: a row is reproducible only if the count is recorded
# ---------------------------------------------------------------------------


def test_rows_stamp_the_blas_thread_pin(monkeypatch) -> None:
    """The optimum moves with the thread count, so the count the fit ran under
    lands on every row -- and survives the from_rows round-trip."""
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "1")
    surf = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",),
                             rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
    rows = sf.rows_from_surfaces({"SPXW": surf})
    assert {r["blas_threads"] for r in rows} == {1}
    loaded = sf.Surface.from_rows(rows)
    assert {s.blas_threads for s in loaded.slices} == {1}


def test_rows_record_a_deliberate_override(monkeypatch) -> None:
    """cronjob.sh leaves an operator's override in place; the stamp must record
    the configured value, not the scheduled default. This pins the env-to-row
    plumbing only -- that the configured value IS the count the loaded backend
    uses is the subprocess tests' claim (tests/test_thread_pin.py), where the
    env is fixed before OpenBLAS loads; nothing mutates it in-process."""
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "4")
    surf = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",),
                             rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
    assert {r["blas_threads"] for r in sf.rows_from_surfaces({"SPXW": surf})} == {4}


def test_rows_record_null_when_unpinned(monkeypatch) -> None:
    """An unpinned run is honestly not reproducible; the row records null
    rather than guessing a count."""
    monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
    surf = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",),
                             rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
    rows = sf.rows_from_surfaces({"SPXW": surf})
    assert {r["blas_threads"] for r in rows} == {None}
    assert {s.blas_threads for s in sf.Surface.from_rows(rows).slices} == {None}


def test_from_rows_refuses_rows_without_the_thread_pin_column() -> None:
    """A partition written before the stamp existed must fail loud, not read
    as an unpinned fit -- same contract as the daycount stamp."""
    surf = sf.build_surfaces(_svi_bars(), DAY, roots=("SPXW",),
                             rate_fn=_flat_rate, daycount=ACT_365)["SPXW"]
    rows = sf.rows_from_surfaces({"SPXW": surf})
    for r in rows:
        del r["blas_threads"]
    with pytest.raises(sf.SurfaceError, match="no blas_threads column"):
        sf.Surface.from_rows(rows)
