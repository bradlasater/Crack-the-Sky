"""SVI smile fit per (date, root, expiry): the strike dimension.

``pricing.term_structure`` reduces the chain to the ATM curve -- one row per
(date, root, expiry). This module adds the strike dimension on top of it: a
raw-SVI (Gatheral) fit of total implied variance ``w(k) = iv^2 * T`` against
log-moneyness ``k = ln(K / F)`` for every expiry with enough quoting strikes,
and a :class:`Surface` that evaluates the fitted slices at any ``(K, T)``.

**Same data, same forward, same inversion.** The chain comes from
``option_day_bars`` through the same helpers the ATM curve uses
(:func:`pricing.term_structure.bars_to_chain`, ``forward_from_parity`` and the
Black-76 inversion of ``term_structure._invert``), so a slice and its ATM row
are built from identical closes and an identical F. ``vol(K_atm, T)`` and
``atm_term_structure.atm_iv`` agreeing is then a cross-check between two
datasets, not a calibration target. The surface sits *alongside* the ATM
curve; nothing here replaces it.

**SPX/SPXW only.** SPY strikes are American and invert under the wrong
exercise boundary with a European solver, so they are refused here. The
American inverter exists (``pricing.iv.implied_vol_american``), but the fit
path in this module is European end to end; an SPY smile is follow-up, not a
flag. VIX options are on the VX future and their smiles are a different
modelling question.

**OTM strikes only.** Calls above F, puts below. OTM options are the liquid
half of the chain and their price is nearly all time value, which is what an
IV inversion needs; a deep-ITM day-bar close is a stale print on what is
mostly a bond.

**Near-worthless closes are dropped before fitting.** An OTM close *is* time
value, and a day bar carries no trade-age signal (the staleness gap of issue
#44), so the staleness proxy is a price floor: an OTM close at or below
``MIN_TIME_VALUE_FRAC`` of the forward -- a few ticks at index scale -- inverts
to an IV dominated by the tick size rather than by the market. On real
sessions those prints are exactly the stale wing closes that drag an
unconstrained fit onto its parameter bounds. The floor scales with F, so it
is deterministic, data-derived, and unit-free; an expiry left with fewer than
MIN_STRIKES strikes after filtering is skipped, same as a thin chain.

**The fit.** Raw SVI, ``w(k) = a + b(rho(k-m) + sqrt((k-m)^2 + sigma^2))``,
with ``scipy.optimize.least_squares`` over the five parameters. The domain
constraint for non-negative total variance, ``a + b*sigma*sqrt(1-rho^2) >= 0``
(that expression *is* the minimum of the curve), is enforced exactly by
fitting that minimum ``w0`` in place of ``a`` under the box bound ``w0 >= 0``
and recovering ``a`` afterwards. The seed is deterministic and data-derived
-- minimum observed w, ATM k for m, wing slopes for b, rho = -0.5, sigma =
0.1 -- so refitting the same slice reproduces the same parameters. The
evaluation budget is raised to ``MAX_NFEV`` because real chains exhaust
scipy's default (500 evals for five parameters) long before the tight
tolerances are met; observed converged fits need ~1200 (issue #46).

**The butterfly guard is enforced inside the fit, not only after it.** The
unconstrained least-squares fit runs first and is accepted unchanged when it
is arbitrage-clean. When it violates -- on real chains the violation sits in
the padded wing past the last quoted strike, where no data constrains the
curve -- the slice is refit by SLSQP minimising the *same* sum of squared
residuals subject to ``g(k) >= G_REPAIR_MARGIN`` on the guard grid as a hard
constraint. Each seed (the unconstrained optimum, then the data-derived
seed) is first projected onto the feasible region along the segment toward
the flat slice -- ``b = 0`` has ``g == 1`` everywhere, so a feasible point
always exists on that segment -- because SLSQP started infeasible slides to
the degenerate flat slice instead of the good feasible fit next to the data.
The feasible candidate with the lower cost wins.

**The calendar guard is chained through the build the same way.** Real day
bars carry small genuine calendar inversions -- the raw ATM curve itself
crosses on these sessions -- so the build fits slices in expiry order and a
slice whose unconstrained fit dips below its predecessor's total variance on
the calendar guard's union grid is refit with ``w(k) >= w_prev(k)`` on that
grid as a second hard constraint. The union grid is known before any fitting
(filtering and eligibility are data-determined), so the constraint covers
exactly the domain :class:`Surface` re-checks afterwards.

**Repairs must still explain the slice.** A constrained refit is accepted
only while its rms stays within ``REPAIR_MAX_REL_RMS`` of the mean total
variance; past that the arbitrage is in the data, not in wing noise, and
repairing would be fabrication. A slice that still violates after the
constrained refit -- or whose only feasible refits are rejected by that bound
-- raises :class:`SurfaceArbitrageError` via the unchanged post-fit guards
(per-slice butterfly in :func:`fit_slice`, cross-slice calendar in
:class:`Surface`): a genuinely broken smile, not noise. Fail-loud is kept;
what changed is that noise is repaired, not fatal.

**Evaluation.** :meth:`Surface.vol` interpolates *linearly in total variance*
between the bracketing expiries -- the arb-preserving choice -- each slice at
its own forward's log-moneyness, and holds the nearest slice flat outside the
fitted term range. On a fitted expiry's own T it returns that slice exactly.

Run: ``python -m pricing.surface [--date YYYY-MM-DD] [--underlying SPX,SPXW]``
(default date: the previous trading day). Scheduled Tue–Sat 12:15 ET as
``massive-surface`` (after ``term_structure`` at 12:00, before
``coverage_audit`` at 12:30). For the archive, use ``scripts/build_surface.py``.
:func:`load_surface` / :meth:`Surface.from_rows` read the landed
``vol_surface`` parameters back; consumers must not refit from day bars.
"""

from __future__ import annotations

import bisect
import math
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import Bounds, NonlinearConstraint, least_squares, minimize

from ingest.common import landing, market_gate
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.common.rates import load_curve, rate_for
from ingest.jobs import forward_from_parity, parse_underlyings
from pricing.daycount import DEFAULT_DAYCOUNT, DayCount, discount_year_fraction
from pricing.term_structure import (
    _invert,
    _legs_by_expiry,
    bars_to_chain,
    read_day_bars,
)

JOB = "surface"
DATASET = "vol_surface"
SRC = "day_bars"

# SPY is American and VIX smiles live on the VX future; only the European
# index roots have strikes comparable under one BSM inversion.
SURFACE_ROOTS = ("SPX", "SPXW")

# Five SVI parameters want at least five points.
MIN_STRIKES = 5

# Real chains exhaust scipy's default least_squares budget (500 evals for
# five parameters) before the tight tolerances are met; converged retries
# need ~1200 (issue #46). The tolerances stay at 1e-12 -- the flat-slice
# contract (b driven onto its zero bound) depends on them.
MAX_NFEV = 5000

# Pre-filter floor: an OTM close at or below this fraction of the forward is
# within a few ticks of worthless, and its IV inversion is tick-dominated
# noise (see the module docstring). 3e-5 is ~$0.23 at F ~ 7700.
MIN_TIME_VALUE_FRAC = 3e-5

# Butterfly guard grid: g(k) is checked this far (in k) past the quoted
# strikes. Calendar comparison shares the grid resolution.
G_PAD = 1.0
G_POINTS = 201
G_TOL = 1e-8
# The constrained refit is required to beat zero by this margin so the
# guard's G_TOL check afterwards passes comfortably.
G_REPAIR_MARGIN = 1e-6
CAL_TOL = 1e-10

# A repair is accepted only when it still explains the slice: rms within this
# multiple of the mean total variance. Past it, the arbitrage is in the data
# itself, not in wing noise -- the repair would be fabrication, so the guards
# raise instead. Real-session repairs land under 0.12 (issue #46); a smile
# whose data is itself arbitrageable (the calendar test's flat 0.30 vs 0.10
# slices) needs ~1.2 and is rejected.
REPAIR_MAX_REL_RMS = 0.5


class SurfaceError(RuntimeError):
    """A session or slice cannot yield a surface: thin chain or solver failure."""


class SurfaceArbitrageError(SurfaceError):
    """A fit violates butterfly (g(k) < 0) or calendar (w decreasing in T)."""


def _svi_w(k: Any, a: float, b: float, rho: float, m: float, sigma: float) -> Any:
    """Raw-SVI total implied variance at log-moneyness ``k`` (vectorized)."""
    d = np.asarray(k, dtype=float) - m
    return a + b * (rho * d + np.sqrt(d * d + sigma * sigma))


def _g(k: np.ndarray, a: float, b: float, rho: float, m: float, sigma: float) -> np.ndarray:
    """Gatheral's g(k): the implied density is non-negative iff g(k) >= 0.

    w is floored at 1e-12: the domain constraint permits a slice whose minimum
    sits exactly on zero, and g has 1/w terms.
    """
    d = k - m
    sq = np.sqrt(d * d + sigma * sigma)
    w = np.maximum(a + b * (rho * d + sq), 1e-12)
    dw = b * (rho + d / sq)
    d2w = b * sigma * sigma / (sq * sq * sq)
    return (1.0 - k * dw / (2.0 * w)) ** 2 - (dw * dw / 4.0) * (1.0 / w + 0.25) + d2w / 2.0


def _butterfly_grid(k_lo: float, k_hi: float) -> np.ndarray:
    """The padded k-grid the butterfly guard and the constrained refit share."""
    return np.linspace(k_lo - G_PAD, k_hi + G_PAD, G_POINTS)


def _min_g(grid: np.ndarray, p: np.ndarray) -> float:
    """Smallest g(k) on the grid for fit parameters (w0, b, rho, m, sigma)."""
    w0, b, rho, m, sigma = (float(v) for v in p)
    a = w0 - b * sigma * math.sqrt(1.0 - rho * rho)
    return float(np.min(_g(grid, a, b, rho, m, sigma)))


def _check_butterfly(
    a: float, b: float, rho: float, m: float, sigma: float, k_lo: float, k_hi: float
) -> float:
    """Min of g(k) on the padded grid; SurfaceArbitrageError when negative."""
    grid = _butterfly_grid(k_lo, k_hi)
    min_g = float(np.min(_g(grid, a, b, rho, m, sigma)))
    if min_g < -G_TOL:
        raise SurfaceArbitrageError(
            f"butterfly arbitrage: min g(k) = {min_g:.6g} over "
            f"[{k_lo - G_PAD:.3f}, {k_hi + G_PAD:.3f}] for "
            f"a={a:.6g} b={b:.6g} rho={rho:.4f} m={m:.4f} sigma={sigma:.4f}"
        )
    return min_g


def _svi_w_jac(ks: np.ndarray, p: np.ndarray) -> np.ndarray:
    """Jacobian of ``w(k; p)`` wrt ``p = (w0, b, rho, m, sigma)``; vectorized.

    ``w0`` stands in for ``a`` (see :func:`fit_slice`), so the derivatives of
    ``a = w0 - b*sigma*sqrt(1-rho^2)`` fold into the b/rho/sigma columns.
    """
    _w0, b, rho, m, sigma = (float(v) for v in p)
    d = ks - m
    q = np.sqrt(d * d + sigma * sigma)
    s = math.sqrt(1.0 - rho * rho)
    return np.column_stack([
        np.ones_like(ks),
        rho * d + q - sigma * s,
        b * d + b * sigma * rho / s,
        b * (-rho - d / q),
        b * sigma / q - b * s,
    ])


def _project_to_feasible(
    p0: np.ndarray, ws: np.ndarray, grid: np.ndarray,
    lower: np.ndarray, upper: np.ndarray,
    w_floor: np.ndarray | None = None, cal_grid: np.ndarray | None = None,
) -> np.ndarray:
    """Nearest point on the p0-to-flat segment that clears the guard margins.

    The flat slice (b = 0) has ``g == 1`` everywhere, and at a level above the
    calendar floor plus the margin it clears both guards, so some point of the
    segment qualifies; the scan keeps the one closest to p0, i.e. the most
    data-faithful feasible start. The one exception -- a calendar floor
    peaking within the margin of the w0 box bound, where no flat seed is
    feasible -- raises :class:`SurfaceArbitrageError` rather than returning an
    infeasible start. SLSQP needs this: started infeasible it slides to the
    degenerate flat slice rather than the good feasible fit next to the data.
    Deterministic, like the seed itself.
    """
    level = float(np.mean(ws))
    if w_floor is not None:
        # The anchor must clear the floor by the margin, not sit on it: at
        # exactly max(w_floor) the calendar difference is 0 there, and ok()
        # requires the margin so the seed starts strictly feasible rather
        # than on the constraint boundary.
        level = max(level, float(np.max(w_floor)) + 2 * G_REPAIR_MARGIN)
    flat = np.clip(np.array([level, 0.0, 0.0, 0.0, 0.1]), lower, upper)

    def ok(p: np.ndarray) -> bool:
        if _min_g(grid, p) < G_REPAIR_MARGIN:
            return False
        if w_floor is not None:
            w0, b, rho, m, sigma = (float(v) for v in p)
            a = w0 - b * sigma * math.sqrt(1.0 - rho * rho)
            if float(np.min(_svi_w(cal_grid, a, b, rho, m, sigma) - w_floor)) < G_REPAIR_MARGIN:
                return False
        return True

    if ok(p0):
        return p0
    if not ok(flat):
        # Reachable only when the calendar floor's maximum is within the
        # margin of the w0 box bound: the flat slice has the highest minimum
        # variance on the segment (any b > 0 dips below its w0), so no seed
        # on it is feasible either. Fail loudly rather than hand SLSQP the
        # infeasible start this projection exists to avoid.
        raise SurfaceArbitrageError(
            f"calendar floor peaks at {float(np.max(w_floor)):.6g}, within the "
            f"repair margin of the w0 bound {upper[0]:g}: no feasible seed exists"
        )
    for t in np.linspace(0.05, 1.0, 20):
        p = (1.0 - t) * p0 + t * flat
        if ok(p):
            return p
    return flat


def _repair_fit(
    ks: np.ndarray, ws: np.ndarray, residuals: Any, grid: np.ndarray,
    lower: np.ndarray, upper: np.ndarray, seed: np.ndarray, x0: np.ndarray,
    w_floor: np.ndarray | None = None, cal_grid: np.ndarray | None = None,
) -> np.ndarray | None:
    """Constrained refit: same objective, plus the guards as hard constraints.

    Butterfly: g(k) >= G_REPAIR_MARGIN on this slice's padded guard grid.
    Calendar (when the build path chains a previous slice): w(k) >= w_prev(k)
    on the calendar guard's union grid. Runs SLSQP (analytic objective
    Jacobian, 3-point finite-difference constraint Jacobian) from the
    feasibility-projected unconstrained optimum and the feasibility-projected
    data seed, and keeps the feasible candidate with the lower cost. Returns
    None when neither run reaches feasibility -- :func:`fit_slice` then lets
    the post-fit guards raise.
    """
    def objective(p: np.ndarray) -> float:
        r = residuals(p)
        return 0.5 * float(np.dot(r, r))

    def objective_jac(p: np.ndarray) -> np.ndarray:
        return _svi_w_jac(ks, p).T @ residuals(p)

    def constraint(p: np.ndarray) -> np.ndarray:
        w0, b, rho, m, sigma = (float(v) for v in p)
        a = w0 - b * sigma * math.sqrt(1.0 - rho * rho)
        parts = [_g(grid, a, b, rho, m, sigma)]
        if w_floor is not None:
            parts.append(_svi_w(cal_grid, a, b, rho, m, sigma) - w_floor)
        return np.concatenate(parts)

    def feasible(p: np.ndarray) -> bool:
        if _min_g(grid, p) < -G_TOL:
            return False
        if w_floor is not None:
            w0, b, rho, m, sigma = (float(v) for v in p)
            a = w0 - b * sigma * math.sqrt(1.0 - rho * rho)
            if float(np.min(_svi_w(cal_grid, a, b, rho, m, sigma) - w_floor)) < -CAL_TOL:
                return False
        return True

    # The margin applies to the butterfly rows only. Calendar rows need just
    # w >= w_prev: broadcasting the margin to them would cumulatively lift
    # total variance across chained repairs and can push an otherwise clean
    # repair past the relative-RMS acceptance bound.
    lb = np.full(len(grid), G_REPAIR_MARGIN)
    if w_floor is not None:
        lb = np.concatenate([lb, np.zeros(len(cal_grid))])
    guard = NonlinearConstraint(constraint, lb, np.inf, jac="3-point")
    candidates: list[tuple[float, np.ndarray]] = []
    for start in (x0, seed):
        res = minimize(
            objective,
            _project_to_feasible(start, ws, grid, lower, upper, w_floor, cal_grid),
            method="SLSQP", jac=objective_jac, bounds=Bounds(lower, upper),
            constraints=[guard], options={"ftol": 1e-12, "maxiter": 1000},
        )
        if feasible(res.x):
            candidates.append((objective(res.x), res.x))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


@dataclass(frozen=True)
class SliceFit:
    """The five raw-SVI parameters plus fit diagnostics for one expiry."""

    a: float
    b: float
    rho: float
    m: float
    sigma: float
    rms_error: float
    min_g: float


def fit_slice(
    ks: Any, ws: Any,
    floor: Any = None, cal_grid: np.ndarray | None = None,
) -> SliceFit:
    """Raw-SVI fit of one expiry's total variances; loud on thin input.

    ``ks`` is log-moneyness ``ln(K/F)``, ``ws`` the matching total implied
    variances ``iv^2 * T``. Raises :class:`SurfaceError` for fewer than
    MIN_STRIKES points or a solver failure, and :class:`SurfaceArbitrageError`
    when even the arbitrage-constrained refit prices a negative density.

    The build path chains slices in expiry order: ``floor`` is the previously
    fitted slice (anything with the five SVI attributes -- a :class:`Slice`
    or :class:`SliceFit`) and ``cal_grid`` the calendar guard's union grid,
    so a slice whose fit dips below its predecessor's total variance is refit
    under that constraint too. Standalone callers (no floor) get the
    butterfly guard only; the calendar backstop lives in :class:`Surface`
    either way.
    """
    ks = np.asarray(ks, dtype=float)
    ws = np.asarray(ws, dtype=float)
    if ks.size != ws.size:
        raise SurfaceError(f"k and w lengths differ: {ks.size} vs {ws.size}")
    if ks.size < MIN_STRIKES:
        raise SurfaceError(
            f"need at least {MIN_STRIKES} strikes to fit five SVI parameters, "
            f"got {ks.size}"
        )
    if not (np.all(np.isfinite(ks)) and np.all(np.isfinite(ws)) and np.all(ws > 0)):
        raise SurfaceError("k and w must be finite with w > 0")

    order = np.argsort(ks)
    ks = ks[order]
    ws = ws[order]

    # Deterministic, data-derived seed: a refit of the same slice reproduces
    # the same parameters. The wing slopes give b -- asymptotically dw/dk is
    # b(1+rho) on the right and -b(1-rho) on the left, so (right-left)/2 = b.
    left = float((ws[1] - ws[0]) / (ks[1] - ks[0]))
    right = float((ws[-1] - ws[-2]) / (ks[-1] - ks[-2]))
    seed = np.array([
        max(float(ws.min()), 1e-8),        # w0 ~= the curve's minimum
        max((right - left) / 2.0, 1e-4),   # b from the wing slopes
        -0.5,                              # rho: index skew is negative
        float(ks[np.argmin(np.abs(ks))]),  # m at the strike nearest F
        0.1,                               # sigma
    ])
    lower = np.array([0.0, 0.0, -0.999, -3.0, 1e-4])
    upper = np.array([10.0, 10.0, 0.999, 3.0, 5.0])
    seed = np.clip(seed, lower, upper)

    def residuals(p: np.ndarray) -> np.ndarray:
        w0, b, rho, m, sigma = p
        # Fit w0 = a + b*sigma*sqrt(1-rho^2) -- the minimum of w(k) -- in
        # place of a, so the domain constraint w(k) >= 0 for all k is exactly
        # the box bound w0 >= 0 rather than a penalty a solver can slip past.
        a = w0 - b * sigma * math.sqrt(1.0 - rho * rho)
        return _svi_w(ks, a, b, rho, m, sigma) - ws

    try:
        # Tight tolerances: a flat slice must drive b onto its zero bound,
        # not park at the seed because the gradient fell under gtol.
        fit = least_squares(residuals, seed, bounds=(lower, upper),
                            ftol=1e-12, xtol=1e-12, gtol=1e-12,
                            max_nfev=MAX_NFEV)
    except Exception as exc:  # noqa: BLE001 - a solver failure is a fit failure
        raise SurfaceError(f"SVI least_squares raised: {exc}") from exc
    if not fit.success:
        raise SurfaceError(f"SVI fit did not converge: {fit.message}")

    x = fit.x
    grid = _butterfly_grid(float(ks[0]), float(ks[-1]))
    w_floor = None
    if floor is not None and cal_grid is not None:
        w_floor = _svi_w(cal_grid, floor.a, floor.b, floor.rho, floor.m, floor.sigma)

    def violates(p: np.ndarray) -> bool:
        if _min_g(grid, p) < -G_TOL:
            return True
        if w_floor is not None:
            w0, b, rho, m, sigma = (float(v) for v in p)
            a = w0 - b * sigma * math.sqrt(1.0 - rho * rho)
            if float(np.min(_svi_w(cal_grid, a, b, rho, m, sigma) - w_floor)) < -CAL_TOL:
                return True
        return False

    if violates(x):
        # The unconstrained optimum is arbitrageable -- on real chains this is
        # wing noise (the violation sits past the last quoted strike, where no
        # data constrains the curve) or a small calendar crossing the day bars
        # themselves carry. Refit under the guards as hard constraints rather
        # than failing the slice outright, and keep the repair only while it
        # still explains the slice (REPAIR_MAX_REL_RMS): past that, the
        # arbitrage is genuine and the guards below raise.
        repaired = _repair_fit(ks, ws, residuals, grid, lower, upper, seed, x,
                               w_floor, cal_grid)
        if repaired is not None:
            r = float(np.sqrt(np.mean(residuals(repaired) ** 2)))
            if r <= REPAIR_MAX_REL_RMS * float(np.mean(ws)):
                x = repaired

    w0, b, rho, m, sigma = (float(v) for v in x)
    a = w0 - b * sigma * math.sqrt(1.0 - rho * rho)
    rms = float(np.sqrt(np.mean(residuals(x) ** 2)))
    # The guard still raises after the fit -- the loud backstop for a slice
    # the constrained refit could not repair either.
    min_g = _check_butterfly(a, b, rho, m, sigma, float(ks[0]), float(ks[-1]))
    return SliceFit(a=a, b=b, rho=rho, m=m, sigma=sigma, rms_error=rms, min_g=min_g)


@dataclass(frozen=True)
class Slice:
    """One fitted expiry: raw SVI over ``k = ln(K/F)``, ``w = iv^2 * T``."""

    expiration_date: str
    dte: int
    t_years: float
    forward: float
    a: float
    b: float
    rho: float
    m: float
    sigma: float
    k_min: float
    k_max: float
    n_strikes: int
    rms_error: float
    min_g: float
    rate: float

    def total_variance(self, k: float) -> float:
        d = k - self.m
        return self.a + self.b * (self.rho * d + math.hypot(d, self.sigma))

    def vol(self, K: float) -> float:
        """Implied vol at strike ``K`` on this slice's own expiry."""
        if K <= 0:
            raise ValueError(f"strike must be positive, got {K}")
        return math.sqrt(self.total_variance(math.log(K / self.forward)) / self.t_years)


class Surface:
    """The fitted slices of one (date, root): calendar-checked, evaluable.

    Construction is the calendar guard: total variance must be non-decreasing
    in T at every k across the fitted slices, else SurfaceArbitrageError.
    """

    def __init__(self, d: date, underlying: str, slices: list[Slice]) -> None:
        if not slices:
            raise SurfaceError(f"no fitted slices for {underlying} on {d}")
        self.date = d.isoformat()
        self.underlying = underlying
        self.slices = sorted(slices, key=lambda s: s.t_years)
        self._check_calendar()

    def _check_calendar(self) -> None:
        lo = min(s.k_min for s in self.slices)
        hi = max(s.k_max for s in self.slices)
        grid = np.linspace(lo, hi, G_POINTS)
        for earlier, later in zip(self.slices, self.slices[1:], strict=False):
            diff = (
                _svi_w(grid, later.a, later.b, later.rho, later.m, later.sigma)
                - _svi_w(grid, earlier.a, earlier.b, earlier.rho, earlier.m, earlier.sigma)
            )
            worst = float(np.min(diff))
            if worst < -CAL_TOL:
                raise SurfaceArbitrageError(
                    f"calendar arbitrage: total variance drops {abs(worst):.6g} at "
                    f"k={float(grid[np.argmin(diff)]):.4f} between "
                    f"{earlier.expiration_date} (T={earlier.t_years:.4f}) and "
                    f"{later.expiration_date} (T={later.t_years:.4f})"
                )

    def vol(self, K: float, T: float) -> float:
        """Implied vol at strike ``K`` and time ``T`` years (ACT/365).

        Linear in total variance between the bracketing expiries, each slice
        evaluated at its own forward's log-moneyness; the nearest slice is
        held flat outside the fitted term range. T landing exactly on a fitted
        expiry returns that slice, bit-for-bit.
        """
        if K <= 0 or T <= 0:
            raise ValueError(f"K and T must be positive, got K={K} T={T}")
        ts = [s.t_years for s in self.slices]
        if ts[0] >= T:
            return self.slices[0].vol(K)
        if ts[-1] <= T:
            return self.slices[-1].vol(K)
        i = bisect.bisect_right(ts, T)  # ts[i-1] < T <= ts[i]
        lo, hi = self.slices[i - 1], self.slices[i]
        w_lo = lo.total_variance(math.log(K / lo.forward))
        w_hi = hi.total_variance(math.log(K / hi.forward))
        w = w_lo + (w_hi - w_lo) * (T - lo.t_years) / (hi.t_years - lo.t_years)
        return math.sqrt(w / T)

    def __len__(self) -> int:
        return len(self.slices)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Surface({self.underlying} {self.date}, {len(self.slices)} slices)"

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> Surface:
        """Project landed ``vol_surface`` records into a calendar-checked Surface.

        ``Slice`` fields are the schema columns with the ``svi_`` prefix
        stripped from the five parameters. ``date`` / ``underlying`` / ``src``
        are Surface-level (or write-only). Mixed dates or roots fail loud --
        a partition holds every root for the session, and each Surface is
        one root. Construction still runs :meth:`_check_calendar`, so a
        tampered parquet cannot evaluate as a smile.
        """
        if not rows:
            raise SurfaceError("no vol_surface rows")
        dates = {str(r["date"])[:10] for r in rows}
        underlyings = {str(r["underlying"]) for r in rows}
        if len(dates) != 1 or len(underlyings) != 1:
            raise SurfaceError(
                f"from_rows expects one (date, underlying), got "
                f"dates={sorted(dates)} underlyings={sorted(underlyings)}"
            )
        d = date.fromisoformat(dates.pop())
        underlying = underlyings.pop()
        if underlying not in SURFACE_ROOTS:
            raise SurfaceError(
                f"surface fits the European index roots {SURFACE_ROOTS}, "
                f"got {underlying!r}"
            )
        slices = [_slice_from_row(r) for r in rows]
        expiries = [s.expiration_date for s in slices]
        if len(expiries) != len(set(expiries)):
            raise SurfaceError(
                f"duplicate expiries in vol_surface rows for {underlying} on {d}"
            )
        return cls(d, underlying, slices)


def _slice_from_row(row: dict[str, Any]) -> Slice:
    """One landed vol_surface record → Slice. Prefix map is the schema contract."""
    return Slice(
        expiration_date=str(row["expiration_date"])[:10],
        dte=int(row["dte"]),
        t_years=float(row["t_years"]),
        forward=float(row["forward"]),
        a=float(row["svi_a"]),
        b=float(row["svi_b"]),
        rho=float(row["svi_rho"]),
        m=float(row["svi_m"]),
        sigma=float(row["svi_sigma"]),
        k_min=float(row["k_min"]),
        k_max=float(row["k_max"]),
        n_strikes=int(row["n_strikes"]),
        rms_error=float(row["rms_error"]),
        min_g=float(row["min_g"]),
        rate=float(row["rate"]),
    )


def _expiry_points(
    F: float, T: float, r: float, legs: dict[float, dict[str, float]],
) -> tuple[list[float], list[float]]:
    """One expiry's filtered OTM (k, w) points, sorted by k.

    Calls above F and puts below -- the liquid half of the chain, and the half
    whose close is time value rather than a discounted intrinsic. OTM closes
    at or below the MIN_TIME_VALUE_FRAC time-value floor are dropped before
    inversion (see the module docstring).
    """
    ks: list[float] = []
    ws: list[float] = []
    for K, leg in sorted(legs.items()):
        if K > F:
            kind = "call"
        elif K < F:
            kind = "put"
        else:  # a strike exactly on the forward belongs to neither wing
            continue
        px = leg.get(kind)
        if px is None or px <= MIN_TIME_VALUE_FRAC * F:
            continue
        iv = _invert(px, F, K, T, r, kind)
        if iv is None:
            continue
        ks.append(math.log(K / F))
        ws.append(iv * iv * T)
    return ks, ws


def _fit_expiry(
    expiry: str, dte: int, T: float, F: float, r: float,
    ks: list[float], ws: list[float],
    floor: Any = None, cal_grid: np.ndarray | None = None,
) -> Slice:
    """Fit one expiry's filtered OTM points and assemble its Slice.

    ``floor``/``cal_grid`` chain the calendar constraint from the previously
    fitted slice; see :func:`fit_slice`.
    """
    fit = fit_slice(ks, ws, floor=floor, cal_grid=cal_grid)
    return Slice(
        expiration_date=expiry, dte=dte, t_years=T, forward=F,
        a=fit.a, b=fit.b, rho=fit.rho, m=fit.m, sigma=fit.sigma,
        k_min=ks[0], k_max=ks[-1], n_strikes=len(ks),
        rms_error=fit.rms_error, min_g=fit.min_g, rate=r,
    )


def build_surfaces(
    bars: list[dict[str, Any]],
    d: date,
    roots: tuple[str, ...] = SURFACE_ROOTS,
    data_root: Path | str | None = None,
    rate_fn: Any = None,
    daycount: DayCount = DEFAULT_DAYCOUNT,
) -> dict[str, Surface]:
    """One Surface per root for a session; pure, so it is testable.

    ``rate_fn(as_of, T) -> float`` is injectable so tests need no rates
    warehouse; it defaults to the landed Treasury curve. Roots outside
    SURFACE_ROOTS are refused rather than fit under the wrong model.
    ``daycount`` decides vol time only -- the rate tenor is money time and
    stays ACT/365 whatever is passed. It also sets the T the calendar-arbitrage
    guard chains on, so every slice in one fit must share it.
    """
    bad = [r for r in roots if r not in SURFACE_ROOTS]
    if bad:
        raise SurfaceError(
            f"surface fits the European index roots {SURFACE_ROOTS}, got {bad}"
        )
    if rate_fn is None:
        def rate_fn(as_of: date, T: float) -> float:  # noqa: ANN001
            return rate_for(as_of, T, data_root)

    surfaces: dict[str, Surface] = {}
    for root in roots:
        chain = bars_to_chain(bars, root)
        if not chain:
            continue

        def _rate_for_expiry(expiry: date) -> float:
            return rate_fn(d, discount_year_fraction(d, expiry))

        forwards = forward_from_parity(chain, _rate_for_expiry, asof_date=d)
        legs = _legs_by_expiry(chain)

        # Two passes. The first computes each expiry's filtered OTM points,
        # which decides eligibility (>= MIN_STRIKES; thin expiries are skipped
        # -- day bars hold only contracts that traded) and the union k-range
        # the calendar guard checks. The second fits in expiry order with the
        # calendar constraint chained off the previous slice on exactly that
        # grid, so the Surface guard afterwards sees no crossing to raise on.
        pending = []
        for fwd in forwards:
            expiry = date.fromisoformat(str(fwd["expiration_date"])[:10])
            dte = (expiry - d).days
            # T=0 has no vol that reproduces a price -- same skip as the ATM
            # curve's, for the same reason.
            if dte <= 0:
                continue
            T = daycount.year_fraction(d, expiry)
            # dte > 0 stops implying T > 0 once vol time is a session count:
            # a span every day of which is a closure counts zero sessions.
            # 2025-01-09, the day of mourning for President Carter, does it
            # for the Wednesday-to-Thursday span ending on it. Zero is the
            # right answer -- there is no trading left before expiry, so
            # there is no remaining variance -- and it is the same reason the
            # dte guard above exists, so it gets the same treatment rather
            # than a floor, which would invent vol time the calendar denies.
            if T <= 0:
                continue
            F = float(fwd["forward"])
            r = _rate_for_expiry(expiry)
            ks, ws = _expiry_points(F, T, r, legs.get(fwd["expiration_date"], {}))
            if len(ks) < MIN_STRIKES:
                continue
            pending.append((fwd["expiration_date"], dte, T, F, r, ks, ws))

        cal_grid = None
        if pending:
            cal_grid = np.linspace(
                min(p[5][0] for p in pending),
                max(p[5][-1] for p in pending),
                G_POINTS,
            )

        slices: list[Slice] = []
        prev: Slice | None = None
        for expiry, dte, T, F, r, ks, ws in pending:
            sl = _fit_expiry(expiry, dte, T, F, r, ks, ws,
                             floor=prev, cal_grid=cal_grid)
            prev = sl
            slices.append(sl)
        if slices:
            surfaces[root] = Surface(d, root, slices)
    return surfaces


def rows_from_surfaces(surfaces: dict[str, Surface]) -> list[dict[str, Any]]:
    """Flat vol_surface records for the archive write."""
    rows: list[dict[str, Any]] = []
    for root, surface in surfaces.items():
        for s in surface.slices:
            rows.append({
                "date": surface.date,
                "underlying": root,
                "expiration_date": s.expiration_date,
                "dte": s.dte,
                "t_years": s.t_years,
                "forward": s.forward,
                "svi_a": s.a,
                "svi_b": s.b,
                "svi_rho": s.rho,
                "svi_m": s.m,
                "svi_sigma": s.sigma,
                "k_min": s.k_min,
                "k_max": s.k_max,
                "n_strikes": s.n_strikes,
                "rms_error": s.rms_error,
                "min_g": s.min_g,
                "rate": s.rate,
                "src": SRC,
            })
    rows.sort(key=lambda x: (x["underlying"], x["expiration_date"]))
    return rows


def build_for_date(
    settings: Settings, d: date, roots: tuple[str, ...] = SURFACE_ROOTS,
    daycount: DayCount = DEFAULT_DAYCOUNT,
) -> dict[str, Surface]:
    """Read the partition and fit one surface per root.

    The curve is loaded once here rather than per expiry, for the same reason
    as in term_structure: ``load_curve`` scans every rates partition and a
    chain has ~100 expiries.
    """
    curve = load_curve(d, settings.data_root)
    return build_surfaces(
        read_day_bars(settings, d), d, roots, settings.data_root,
        rate_fn=lambda _as_of, T: curve.at(T),
        daycount=daycount,
    )


def write_rows(settings: Settings, d: date, rows: list[dict[str, Any]]) -> Path:
    """Write one session's rows, replacing this job's previous output.

    Same replace-not-append contract as term_structure.write_rows: write_clean
    is append-only, so snapshot the prior files, write, then quarantine only
    the snapshot -- a retry must not leave a partition double-counting every
    (date, root, expiry) key.
    """
    prior = landing.clean_files(DATASET, d, JOB, settings.data_root)
    path = landing.write_clean(DATASET, d, rows, job=JOB, data_root=settings.data_root)
    if prior:
        landing.quarantine_prior(DATASET, d, JOB, settings.data_root, only=prior)
    return path


def load_surface(settings: Settings, d: date, underlying: str) -> Surface:
    """Read one (date, root) smile back from the ``vol_surface`` partition.

    The package writes SVI parameters and must be able to read them; without
    this every consumer would refit from day bars. Missing partition, empty
    root, or a parquet that fails the calendar guard raises
    :class:`SurfaceError` / :class:`SurfaceArbitrageError` -- no silent
    fallback to a refit.
    """
    import pyarrow.parquet as pq

    if underlying not in SURFACE_ROOTS:
        raise SurfaceError(
            f"surface fits the European index roots {SURFACE_ROOTS}, "
            f"got {underlying!r}"
        )
    part = Path(settings.data_root) / "clean" / DATASET / f"dt={d.isoformat()}"
    paths = sorted(part.glob("*.parquet")) if part.is_dir() else []
    if not paths:
        raise SurfaceError(f"no {DATASET} partition for {d}")
    rows: list[dict[str, Any]] = []
    for path in paths:
        rows.extend(pq.read_table(path).to_pylist())
    filtered = [r for r in rows if r.get("underlying") == underlying]
    if not filtered:
        raise SurfaceError(f"no {DATASET} rows for {underlying} on {d}")
    # Filter is by root only; from_rows then accepts any single date. A
    # misplaced partition whose rows are all dated some other session would
    # otherwise reconstruct and answer as that other session.
    row_dates = {str(r["date"])[:10] for r in filtered}
    want = d.isoformat()
    if row_dates != {want}:
        raise SurfaceError(
            f"{DATASET} rows for {underlying} in dt={want} have "
            f"date={sorted(row_dates)}"
        )
    return Surface.from_rows(filtered)


def _main_fn(args, settings: Settings, logger: JsonlLogger):
    d = date.fromisoformat(args.date)
    roots = tuple(parse_underlyings(args.underlying, list(SURFACE_ROOTS)))

    surfaces = build_for_date(settings, d, roots)
    if not surfaces:
        raise SurfaceError(
            f"no surface for {d}: no option_day_bars, or no expiry quoting at "
            f"least {MIN_STRIKES} OTM strikes for roots {list(roots)}"
        )

    rows = rows_from_surfaces(surfaces)
    by_root = {root: len(s.slices) for root, s in surfaces.items()}
    logger.log("surface", date=d.isoformat(), slices=len(rows), by_root=by_root)
    for root in sorted(surfaces):
        for s in surfaces[root].slices:
            print(f"  {root} {s.expiration_date} dte={s.dte} n={s.n_strikes} "
                  f"a={s.a:.6g} b={s.b:.6g} rho={s.rho:+.4f} m={s.m:+.4f} "
                  f"sigma={s.sigma:.4f} rms={s.rms_error:.3g} min_g={s.min_g:.4f}",
                  file=sys.stderr)
    print(f"PASS  {len(rows)} slices  "
          + "  ".join(f"{k}={v}" for k, v in sorted(by_root.items())), file=sys.stderr)

    if not args.dry_run:
        path = write_rows(settings, d, rows)
        print(f"PASS  wrote {path}", file=sys.stderr)
    return {"rows": len(rows), "roots": len(surfaces)}


def main(argv: list[str] | None = None) -> int:
    """CLI for the scheduled T-1 run; exits 0 on success, 1 on failure.

    Uses ``cli.run_job`` for the same reason term_structure does: the JSONL
    run log, the trading-day gate and the Healthchecks wiring come with it.
    ``--date`` defaults to the previous trading day so the Tue-Sat cron line
    needs no arguments and so a Saturday run gates on Friday's session, the
    same convention ``term_structure`` and ``coverage_audit`` follow.

    Bulk history does *not* come through here -- ``scripts/build_surface.py``
    calls :func:`build_for_date` directly, so backfilling a thousand closed
    days neither pings a monitor nor trips the gate.
    """
    argv = list(argv) if argv is not None else sys.argv[1:]
    if "--date" not in argv:
        prev = market_gate.previous_trading_day(market_gate.today_et())
        argv += ["--date", prev.isoformat()]
    return run_job(JOB, _main_fn, argv)  # run_job exits; return is for tests


if __name__ == "__main__":
    raise SystemExit(main())
