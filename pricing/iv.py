"""Implied volatility: invert an option price → σ for a single contract.

Two inverters, one per exercise style, because the two engines in
:mod:`pricing.engine` need different treatment:

* :func:`implied_vol` inverts European BSM -- Newton with bounds, Brent
  fallback, using the closed-form vega for the Newton step.
* :func:`implied_vol_american` inverts the American CRR tree -- Brent alone,
  since the tree has no closed-form vega to make Newton pay.

The style also moves the no-arbitrage bounds, so each has its own:
:func:`discounted_bounds` and :func:`american_bounds`.

Invalid inputs or a price outside the bounds raise ``ValueError``. Neither
function ever returns NaN.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.optimize import brentq

from pricing.bsm import CallPut, _normalize_cp, price, raw_greeks, resolve_q
from pricing.engine import crr_price

ArrayLike = Any

_VOL_LO = 1e-8
_VOL_HI = 10.0
_NEWTON_ITERS = 50
_PRICE_TOL = 1e-12


def discounted_bounds(
    S: float,
    K: float,
    T: float,
    r: float,
    call_put: CallPut = "call",
    *,
    q: float | None = None,
    F: float | None = None,
) -> tuple[float, float]:
    """``(lower, upper)`` no-arbitrage bounds on the option premium.

    Both bounds are discounted: spot by the dividend yield and strike by the
    rate, i.e. a call sits in ``[max(Se^{-qT} - Ke^{-rT}, 0), Se^{-qT}]``.
    Scalar-only -- the body casts with ``float()``/``bool()`` and would raise
    on arrays, so the signature says so rather than implying it vectorizes.
    """
    S_ = np.asarray(S, dtype=float)
    K_ = np.asarray(K, dtype=float)
    T_ = np.asarray(T, dtype=float)
    r_ = np.asarray(r, dtype=float)
    qv = resolve_q(S_, T_, r_, q=q, F=F)
    disc_s = float(S_ * np.exp(-qv * T_))
    disc_k = float(K_ * np.exp(-r_ * T_))
    is_call = bool(np.asarray(_normalize_cp(call_put)))
    if is_call:
        return max(disc_s - disc_k, 0.0), disc_s
    return max(disc_k - disc_s, 0.0), disc_k


def implied_vol(
    market_price: ArrayLike,
    S: ArrayLike,
    K: ArrayLike,
    T: ArrayLike,
    r: ArrayLike,
    call_put: CallPut | ArrayLike = "call",
    *,
    q: ArrayLike | None = None,
    F: ArrayLike | None = None,
) -> float:
    """Invert European BSM. Raises ``ValueError`` instead of returning NaN."""
    target = float(np.asarray(market_price, dtype=float))
    S_ = float(np.asarray(S, dtype=float))
    K_ = float(np.asarray(K, dtype=float))
    T_ = float(np.asarray(T, dtype=float))
    r_ = float(np.asarray(r, dtype=float))

    if not all(np.isfinite(x) for x in (target, S_, K_, T_, r_)):
        raise ValueError(f"non-finite input: price={market_price!r} S={S_} K={K_} T={T_} r={r_}")
    if S_ <= 0 or K_ <= 0 or T_ <= 0:
        raise ValueError(f"invalid S, K, T: S={S_}, K={K_}, T={T_}")
    if target < 0:
        raise ValueError(f"market_price is negative: {target}")

    qv = float(resolve_q(S_, T_, r_, q=q, F=F))
    lower, upper = discounted_bounds(S_, K_, T_, r_, call_put, q=qv)
    # Tiny slack for floating point; a price outside bounds is not invertible.
    slack = 1e-10 * max(S_, 1.0)
    if target < lower - slack:
        raise ValueError(f"price {target} below intrinsic bound {lower} (S={S_} K={K_} T={T_})")
    if target > upper + slack:
        raise ValueError(f"price {target} above max bound {upper} (S={S_} K={K_} T={T_})")
    if target <= lower + slack:
        return 0.0

    def model(vol: float) -> float:
        return float(price(S_, K_, T_, r_, vol, call_put, q=qv))

    # Brenner–Subrahmanyam-style seed, clipped into the search bracket.
    disc_s = S_ * np.exp(-qv * T_)
    seed = float(np.sqrt(2.0 * np.pi / T_) * (target / max(disc_s, 1e-12)))
    vol = float(np.clip(seed, 1e-3, 5.0))

    for _ in range(_NEWTON_ITERS):
        px = model(vol)
        if abs(px - target) < _PRICE_TOL:
            return vol
        vega = float(raw_greeks(S_, K_, T_, r_, vol, call_put, q=qv)["vega"])
        if vega < 1e-14:
            break
        vol = float(np.clip(vol - (px - target) / vega, _VOL_LO, _VOL_HI))

    def objective(sig: float) -> float:
        return model(sig) - target

    try:
        lo, hi = _VOL_LO, _VOL_HI
        flo, fhi = objective(lo), objective(hi)
        if flo * fhi > 0:
            # Expand / shift the bracket once more around the Newton point.
            lo = max(_VOL_LO, vol / 4.0)
            hi = min(_VOL_HI, max(vol * 4.0, vol + 0.5))
            flo, fhi = objective(lo), objective(hi)
        if flo * fhi > 0:
            raise ValueError(
                f"implied vol bracket does not change sign "
                f"(price={target}, model[{lo}]={flo + target}, "
                f"model[{hi}]={fhi + target})"
            )
        vol = float(brentq(objective, lo, hi, xtol=1e-12, maxiter=200))
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - convert solver failures, never NaN
        raise ValueError(f"implied vol failed: {exc}") from exc

    if not np.isfinite(vol):
        raise ValueError("implied vol solver returned a non-finite value")
    return vol


# ---------------------------------------------------------------------------
# American: invert the CRR tree price -> sigma
# ---------------------------------------------------------------------------

_AMER_VOL_XTOL = 1e-6
_CRR_FLOOR_SAFETY = 1.5
_CRR_VOL_FLOOR_MIN = 1e-6


def american_bounds(
    S: float,
    K: float,
    T: float,
    r: float,
    call_put: CallPut = "call",
    *,
    q: float | None = None,
    F: float | None = None,
) -> tuple[float, float]:
    """``(lower, upper)`` no-arbitrage bounds on an American premium.

    Early exercise moves both ends relative to :func:`discounted_bounds`:

    * The lower bound picks up *undiscounted* intrinsic. A holder can take
      ``S - K`` (call) or ``K - S`` (put) today, so the American value is at
      least that, and at least the European floor as well.
    * The put's upper bound is ``K``, not ``Ke^{-rT}``: exercised immediately
      at ``S -> 0`` it pays ``K`` now, so discounting the cap would place the
      ceiling below prices the contract can actually reach.

    Scalar-only, like :func:`discounted_bounds`.
    """
    S_ = float(np.asarray(S, dtype=float))
    K_ = float(np.asarray(K, dtype=float))
    T_ = float(np.asarray(T, dtype=float))
    r_ = float(np.asarray(r, dtype=float))
    qv = float(resolve_q(S_, T_, r_, q=q, F=F))
    disc_s = S_ * float(np.exp(-qv * T_))
    disc_k = K_ * float(np.exp(-r_ * T_))
    if bool(np.asarray(_normalize_cp(call_put))):
        return max(disc_s - disc_k, S_ - K_, 0.0), S_
    return max(disc_k - disc_s, K_ - S_, 0.0), K_


def crr_vol_floor(T: float, r: float, q: float, n_steps: int) -> float:
    """Smallest sigma at which a CRR tree of ``n_steps`` stays arbitrage-free.

    :func:`pricing.engine.crr_price` raises rather than clipping when the
    risk-neutral probability leaves ``[0, 1]``, which happens below
    ``sigma = |r - q| sqrt(dt)`` -- the tree can no longer straddle the drift.
    The European solver's ``_VOL_LO`` of 1e-8 would therefore raise instead of
    bracketing, so the American search starts here instead, with margin.
    """
    dt = float(T) / int(n_steps)
    return max(_CRR_FLOOR_SAFETY * abs(float(r) - float(q)) * float(np.sqrt(dt)),
               _CRR_VOL_FLOOR_MIN)


def implied_vol_american(
    market_price: ArrayLike,
    S: ArrayLike,
    K: ArrayLike,
    T: ArrayLike,
    r: ArrayLike,
    call_put: CallPut | ArrayLike = "call",
    *,
    q: ArrayLike | None = None,
    F: ArrayLike | None = None,
    n_steps: int = 401,
) -> float:
    """Invert an American CRR price. Raises ``ValueError`` instead of NaN.

    Brent only, with no Newton pre-solve. Newton needs vega, the tree has no
    closed form for it, and a central-difference vega costs two extra trees on
    every step -- more tree evaluations than the bracketed search it was meant
    to accelerate.

    ``n_steps`` must match the tree that will reprice this contract. The
    inverted sigma is the one that reproduces the price *on that tree*; run
    through a finer tree it reprices to a slightly different number, so
    :mod:`pricing.from_market` passes the engine's own step count.

    Accuracy in sigma is bounded by the tree's discretisation error at
    ``n_steps``, not by ``xtol``. Tightening the tolerance past ~1e-6 buys
    precision the model does not have.

    Returns ``0.0`` for a price with no time value, which is a larger set of
    contracts than on the European side. A deep-in-the-money American put is
    optimally exercised now, so the tree returns ``K - S`` for *every* sigma
    and no volatility reproduces the price. That is a real property of the
    contract, not a solver failure, so it reports the same boundary zero the
    European inverter uses -- and callers should treat it the way
    :func:`pricing.term_structure._invert` does, as no information rather
    than as a claim that the market implied no volatility.
    """
    target = float(np.asarray(market_price, dtype=float))
    S_ = float(np.asarray(S, dtype=float))
    K_ = float(np.asarray(K, dtype=float))
    T_ = float(np.asarray(T, dtype=float))
    r_ = float(np.asarray(r, dtype=float))

    if not all(np.isfinite(x) for x in (target, S_, K_, T_, r_)):
        raise ValueError(f"non-finite input: price={market_price!r} S={S_} K={K_} T={T_} r={r_}")
    if S_ <= 0 or K_ <= 0 or T_ <= 0:
        raise ValueError(f"invalid S, K, T: S={S_}, K={K_}, T={T_}")
    if target < 0:
        raise ValueError(f"market_price is negative: {target}")
    if int(n_steps) < 2:
        raise ValueError(f"n_steps must be >= 2, got {n_steps}")

    qv = float(resolve_q(S_, T_, r_, q=q, F=F))
    lower, upper = american_bounds(S_, K_, T_, r_, call_put, q=qv)
    slack = 1e-10 * max(S_, 1.0)
    if target < lower - slack:
        raise ValueError(f"price {target} below intrinsic bound {lower} (S={S_} K={K_} T={T_})")
    if target > upper + slack:
        raise ValueError(f"price {target} above max bound {upper} (S={S_} K={K_} T={T_})")

    cp: CallPut = "call" if bool(np.asarray(_normalize_cp(call_put))) else "put"

    def model(vol: float) -> float:
        return float(
            crr_price(S_, K_, T_, r_, vol, cp, q=qv, n_steps=int(n_steps), american=True)
        )

    def objective(vol: float) -> float:
        return model(vol) - target

    lo = crr_vol_floor(T_, r_, qv, int(n_steps))
    hi = _VOL_HI
    if lo >= hi:
        raise ValueError(
            f"no usable vol bracket at n_steps={n_steps}: the tree is only "
            f"arbitrage-free above sigma={lo}, which is past the {hi} ceiling"
        )

    flo = objective(lo)
    # At the floor the tree is already at its zero-vol value. A price at or
    # under it carries no volatility information -- the same boundary the
    # European solver reports as 0.0, reached from the model rather than from
    # the analytic bound because early exercise moves that floor.
    if flo >= 0.0:
        return 0.0

    fhi = objective(hi)
    if fhi < 0.0:
        raise ValueError(
            f"implied vol bracket does not change sign "
            f"(price={target}, model[{lo}]={flo + target}, model[{hi}]={fhi + target})"
        )

    try:
        vol = float(brentq(objective, lo, hi, xtol=_AMER_VOL_XTOL, maxiter=200))
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - convert solver failures, never NaN
        raise ValueError(f"american implied vol failed: {exc}") from exc

    if not np.isfinite(vol):
        raise ValueError("american implied vol solver returned a non-finite value")
    return vol
