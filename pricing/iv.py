"""Implied volatility: invert BSM price → σ for a single contract.

Newton with bounds, Brent fallback. Invalid inputs or a price outside
no-arbitrage bounds raise ``ValueError``. This function never returns NaN.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.optimize import brentq

from pricing.bsm import CallPut, _normalize_cp, price, raw_greeks, resolve_q

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
