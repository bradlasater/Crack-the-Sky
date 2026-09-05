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
exercise boundary with a European solver, so they are refused until the
American IV solver exists; VIX options are on the VX future and their smiles
are a different modelling question.

**OTM strikes only.** Calls above F, puts below. OTM options are the liquid
half of the chain and their price is nearly all time value, which is what an
IV inversion needs; a deep-ITM day-bar close is a stale print on what is
mostly a bond.

**The fit.** Raw SVI, ``w(k) = a + b(rho(k-m) + sqrt((k-m)^2 + sigma^2))``,
with ``scipy.optimize.least_squares`` over the five parameters. The domain
constraint for non-negative total variance, ``a + b*sigma*sqrt(1-rho^2) >= 0``
(that expression *is* the minimum of the curve), is enforced exactly by
fitting that minimum ``w0`` in place of ``a`` under the box bound ``w0 >= 0``
and recovering ``a`` afterwards. The seed is deterministic and data-derived
-- minimum observed w, ATM k for m, wing slopes for b, rho = -0.5, sigma =
0.1 -- so refitting the same slice reproduces the same parameters.

**Arbitrage guards fail loud.** Each fitted slice is checked for butterfly
arbitrage via Gatheral's ``g(k) >= 0`` on a grid padded past the quoted
strikes (``vol(K, T)`` answers queries out there, and raw SVI's wings are
where a bad fit goes arbitrageable first), and each root's set of slices is
checked for calendar arbitrage -- total variance non-decreasing in T at every
k. A violation raises :class:`SurfaceArbitrageError`: a smile that prices a
negative density or negative forward variance is worse than no smile,
matching marketdata's fail-loud convention.

**Evaluation.** :meth:`Surface.vol` interpolates *linearly in total variance*
between the bracketing expiries -- the arb-preserving choice -- each slice at
its own forward's log-moneyness, and holds the nearest slice flat outside the
fitted term range. On a fitted expiry's own T it returns that slice exactly.

Run: ``python -m pricing.surface [--date YYYY-MM-DD] [--underlying SPX,SPXW]``
(default date: the previous trading day). For the archive, use
``scripts/build_surface.py``. There is deliberately no scheduled job: the
surface is a derived reduction and rebuilds from day bars on demand.
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
from scipy.optimize import least_squares

from ingest.common import landing, market_gate
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.common.rates import load_curve, rate_for
from ingest.jobs import forward_from_parity, parse_underlyings
from pricing.term_structure import (
    DAYS_PER_YEAR,
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

# Butterfly guard grid: g(k) is checked this far (in k) past the quoted
# strikes. Calendar comparison shares the grid resolution.
G_PAD = 1.0
G_POINTS = 201
G_TOL = 1e-8
CAL_TOL = 1e-10


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


def _check_butterfly(
    a: float, b: float, rho: float, m: float, sigma: float, k_lo: float, k_hi: float
) -> float:
    """Min of g(k) on the padded grid; SurfaceArbitrageError when negative."""
    grid = np.linspace(k_lo - G_PAD, k_hi + G_PAD, G_POINTS)
    min_g = float(np.min(_g(grid, a, b, rho, m, sigma)))
    if min_g < -G_TOL:
        raise SurfaceArbitrageError(
            f"butterfly arbitrage: min g(k) = {min_g:.6g} over "
            f"[{k_lo - G_PAD:.3f}, {k_hi + G_PAD:.3f}] for "
            f"a={a:.6g} b={b:.6g} rho={rho:.4f} m={m:.4f} sigma={sigma:.4f}"
        )
    return min_g


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


def fit_slice(ks: Any, ws: Any) -> SliceFit:
    """Raw-SVI fit of one expiry's total variances; loud on thin input.

    ``ks`` is log-moneyness ``ln(K/F)``, ``ws`` the matching total implied
    variances ``iv^2 * T``. Raises :class:`SurfaceError` for fewer than
    MIN_STRIKES points or a solver failure, and :class:`SurfaceArbitrageError`
    when the best fit itself prices a negative density.
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
                            ftol=1e-12, xtol=1e-12, gtol=1e-12)
    except Exception as exc:  # noqa: BLE001 - a solver failure is a fit failure
        raise SurfaceError(f"SVI least_squares raised: {exc}") from exc
    if not fit.success:
        raise SurfaceError(f"SVI fit did not converge: {fit.message}")

    w0, b, rho, m, sigma = (float(v) for v in fit.x)
    a = w0 - b * sigma * math.sqrt(1.0 - rho * rho)
    rms = float(np.sqrt(np.mean(fit.fun ** 2)))
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


def _fit_expiry(
    expiry: str, dte: int, T: float, F: float, r: float,
    legs: dict[float, dict[str, float]],
) -> Slice | None:
    """Fit one expiry's OTM chain; None when too few OTM strikes inverted.

    Calls above F and puts below -- the liquid half of the chain, and the half
    whose close is time value rather than a discounted intrinsic. Thin
    expiries are skipped rather than raised on (day bars hold only contracts
    that traded, so far expiries are legitimately sparse); :func:`fit_slice`
    is the loud primitive when a fit is attempted on too few points.
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
        iv = _invert(leg.get(kind), F, K, T, r, kind)
        if iv is None:
            continue
        ks.append(math.log(K / F))
        ws.append(iv * iv * T)
    if len(ks) < MIN_STRIKES:
        return None

    fit = fit_slice(ks, ws)
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
) -> dict[str, Surface]:
    """One Surface per root for a session; pure, so it is testable.

    ``rate_fn(as_of, T) -> float`` is injectable so tests need no rates
    warehouse; it defaults to the landed Treasury curve. Roots outside
    SURFACE_ROOTS are refused rather than fit under the wrong model.
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
            return rate_fn(d, max((expiry - d).days, 0) / DAYS_PER_YEAR)

        forwards = forward_from_parity(chain, _rate_for_expiry, asof_date=d)
        legs = _legs_by_expiry(chain)

        slices: list[Slice] = []
        for fwd in forwards:
            expiry = date.fromisoformat(str(fwd["expiration_date"])[:10])
            dte = (expiry - d).days
            # T=0 has no vol that reproduces a price -- same skip as the ATM
            # curve's, for the same reason.
            if dte <= 0:
                continue
            sl = _fit_expiry(
                fwd["expiration_date"], dte, dte / DAYS_PER_YEAR,
                float(fwd["forward"]), _rate_for_expiry(expiry),
                legs.get(fwd["expiration_date"], {}),
            )
            if sl is not None:
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
    """CLI for a manual one-session fit; exits 0 on success, 1 on failure.

    Uses ``cli.run_job`` for the same reason term_structure does: the JSONL
    run log, the trading-day gate and the Healthchecks wiring come with it.
    ``--date`` defaults to the previous trading day. Deliberately unscheduled
    -- the issue scopes this to fit + evaluate + tests, and bulk history comes
    from ``scripts/build_surface.py`` calling :func:`build_for_date` directly.
    """
    argv = list(argv) if argv is not None else sys.argv[1:]
    if "--date" not in argv:
        prev = market_gate.previous_trading_day(market_gate.today_et())
        argv += ["--date", prev.isoformat()]
    return run_job(JOB, _main_fn, argv)  # run_job exits; return is for tests


if __name__ == "__main__":
    raise SystemExit(main())
