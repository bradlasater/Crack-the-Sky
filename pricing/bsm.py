"""Black–Scholes–Merton closed-form price and the full named Greek catalog.

Continuous rates ``r`` and dividend yield ``q``. A forward ``F`` is accepted
as an alternative to ``q`` via ``F = S exp((r-q)T)``; this module does not
build a surface or a tenor grid.
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
from scipy.stats import norm

from pricing.conventions import (
    DEFAULT_CONVENTIONS,
    GreeksCatalog,
    GreeksConventions,
    apply_conventions,
)

CallPut = Literal["call", "put", "C", "P", "c", "p"]
ArrayLike = Any


def _f(x: ArrayLike) -> np.ndarray:
    return np.asarray(x, dtype=float)


def resolve_q(
    S: ArrayLike,
    T: ArrayLike,
    r: ArrayLike,
    q: ArrayLike | None = None,
    F: ArrayLike | None = None,
) -> np.ndarray:
    """Dividend yield. Pass ``q`` or ``F``, not both."""
    if F is not None and q is not None:
        raise ValueError("pass q or F, not both")
    if F is not None:
        S_, T_, r_, F_ = np.broadcast_arrays(_f(S), _f(T), _f(r), _f(F))
        return r_ - np.log(F_ / S_) / T_
    if q is None:
        return np.broadcast_arrays(_f(S), _f(0.0))[1]
    return _f(q)


def _normalize_cp(call_put: CallPut | ArrayLike) -> np.ndarray:
    """True where the option is a call."""
    arr = np.asarray(call_put)
    if arr.dtype == bool:
        return arr
    mapping = {"call": True, "C": True, "c": True, "put": False, "P": False, "p": False}

    def one(x: Any) -> bool:
        key = x if isinstance(x, str) else str(x)
        if key not in mapping:
            raise ValueError(f"call_put must be call/put, got {x!r}")
        return mapping[key]

    if arr.ndim == 0:
        return np.asarray(one(arr.item() if hasattr(arr, "item") else call_put))
    return np.vectorize(one, otypes=[bool])(arr)


def _check(S: np.ndarray, K: np.ndarray, T: np.ndarray, sigma: np.ndarray) -> None:
    if np.any(S <= 0) or np.any(K <= 0) or np.any(T <= 0) or np.any(sigma <= 0):
        raise ValueError("S, K, T, sigma must all be strictly positive")


def _maybe_scalar(x: np.ndarray, like: tuple[np.ndarray, ...]) -> Any:
    if all(a.ndim == 0 for a in like) and np.asarray(x).ndim == 0:
        return float(np.asarray(x))
    return np.asarray(x, dtype=float)


def _core(
    S: ArrayLike,
    K: ArrayLike,
    T: ArrayLike,
    r: ArrayLike,
    sigma: ArrayLike,
    *,
    q: ArrayLike | None = None,
    F: ArrayLike | None = None,
) -> dict[str, np.ndarray]:
    S, K, T, r, sigma = (_f(S), _f(K), _f(T), _f(r), _f(sigma))
    qv = resolve_q(S, T, r, q=q, F=F)
    S, K, T, r, sigma, qv = np.broadcast_arrays(S, K, T, r, sigma, qv)
    _check(S, K, T, sigma)
    sqrtT = np.sqrt(T)
    sig_sqrt = sigma * sqrtT
    d1 = (np.log(S / K) + (r - qv + 0.5 * sigma**2) * T) / sig_sqrt
    d2 = d1 - sig_sqrt
    nd1 = norm.pdf(d1)
    nd2 = norm.pdf(d2)
    Nd1 = norm.cdf(d1)
    Nd2 = norm.cdf(d2)
    Nmd1 = norm.cdf(-d1)
    Nmd2 = norm.cdf(-d2)
    disc_q = np.exp(-qv * T)
    disc_r = np.exp(-r * T)
    return {
        "S": S,
        "K": K,
        "T": T,
        "r": r,
        "sigma": sigma,
        "q": qv,
        "d1": d1,
        "d2": d2,
        "nd1": nd1,
        "nd2": nd2,
        "Nd1": Nd1,
        "Nd2": Nd2,
        "Nmd1": Nmd1,
        "Nmd2": Nmd2,
        "disc_q": disc_q,
        "disc_r": disc_r,
        "sqrtT": sqrtT,
    }


def price(
    S: ArrayLike,
    K: ArrayLike,
    T: ArrayLike,
    r: ArrayLike,
    sigma: ArrayLike,
    call_put: CallPut | ArrayLike = "call",
    *,
    q: ArrayLike | None = None,
    F: ArrayLike | None = None,
) -> Any:
    """European BSM price. Vectorized over numpy arrays."""
    c = _core(S, K, T, r, sigma, q=q, F=F)
    is_call = _normalize_cp(call_put)
    call = c["S"] * c["disc_q"] * c["Nd1"] - c["K"] * c["disc_r"] * c["Nd2"]
    put = c["K"] * c["disc_r"] * c["Nmd2"] - c["S"] * c["disc_q"] * c["Nmd1"]
    out = np.where(is_call, call, put)
    return _maybe_scalar(out, (c["S"],))


def raw_greeks(
    S: ArrayLike,
    K: ArrayLike,
    T: ArrayLike,
    r: ArrayLike,
    sigma: ArrayLike,
    call_put: CallPut | ArrayLike = "call",
    *,
    q: ArrayLike | None = None,
    F: ArrayLike | None = None,
) -> dict[str, Any]:
    """Unscaled Greeks: vega per 1.00 vol, theta per year, spot delta/gamma.

    Time derivatives (theta, charm, veta, color) are calendar-time: T decreases.
    """
    c = _core(S, K, T, r, sigma, q=q, F=F)
    is_call = _normalize_cp(call_put)
    S, K, T, r, sig, qv = c["S"], c["K"], c["T"], c["r"], c["sigma"], c["q"]
    d1, d2 = c["d1"], c["d2"]
    nd1, nd2 = c["nd1"], c["nd2"]
    Nd1, Nd2, Nmd1, Nmd2 = c["Nd1"], c["Nd2"], c["Nmd1"], c["Nmd2"]
    dq, dr, sqrtT = c["disc_q"], c["disc_r"], c["sqrtT"]

    call_px = S * dq * Nd1 - K * dr * Nd2
    put_px = K * dr * Nmd2 - S * dq * Nmd1
    px = np.where(is_call, call_px, put_px)

    delta_c = dq * Nd1
    delta_p = -dq * Nmd1
    delta = np.where(is_call, delta_c, delta_p)
    dual_c = -dr * Nd2
    dual_p = dr * Nmd2
    dual_delta = np.where(is_call, dual_c, dual_p)

    vega = S * dq * nd1 * sqrtT
    gamma = dq * nd1 / (S * sig * sqrtT)
    dual_gamma = dr * nd2 / (K * sig * sqrtT)
    vanna = -dq * nd1 * d2 / sig
    volga = vega * d1 * d2 / sig

    theta_c = -S * dq * nd1 * sig / (2.0 * sqrtT) - r * K * dr * Nd2 + qv * S * dq * Nd1
    theta_p = -S * dq * nd1 * sig / (2.0 * sqrtT) + r * K * dr * Nmd2 - qv * S * dq * Nmd1
    theta = np.where(is_call, theta_c, theta_p)

    rho_c = K * T * dr * Nd2
    rho_p = -K * T * dr * Nmd2
    rho = np.where(is_call, rho_c, rho_p)
    psi_c = -T * S * dq * Nd1
    psi_p = T * S * dq * Nmd1
    rho_dividend = np.where(is_call, psi_c, psi_p)

    # charm = ∂Δ/∂t (calendar). ∂d1/∂T = (r-q)/(σ√T) - d2/(2T)
    d1_dT = (r - qv) / (sig * sqrtT) - d2 / (2.0 * T)
    # Δ_c = e^{-qT} N(d1)   =>  ∂Δ_c/∂T = -q e^{-qT} N(d1) + e^{-qT} n(d1) ∂d1/∂T
    # Δ_p = -e^{-qT} N(-d1)  =>  ∂Δ_p/∂T =  q e^{-qT} N(-d1) + e^{-qT} n(d1) ∂d1/∂T
    #
    # Both carry the n(d1)·∂d1/∂T term with a PLUS sign: for the put the two
    # sign flips (the leading minus, and ∂N(-d1)/∂T = -n(d1)·∂d1/∂T) cancel.
    # Getting that wrong is checkable against put-call parity, since
    # Δ_c - Δ_p = e^{-qT} forces ∂Δ_c/∂T - ∂Δ_p/∂T = -q e^{-qT}.
    dDelta_c_dT = -qv * dq * Nd1 + dq * nd1 * d1_dT
    dDelta_p_dT = qv * dq * Nmd1 + dq * nd1 * d1_dT
    charm = np.where(is_call, -dDelta_c_dT, -dDelta_p_dT)

    # veta = ∂vega/∂t (calendar) = -∂vega/∂T
    # ∂vega/∂T / vega = -q - d1 ∂d1/∂T + 1/(2T)
    dVega_dT = vega * (-qv - d1 * d1_dT + 1.0 / (2.0 * T))
    veta = -dVega_dT

    # vera = ∂rho/∂σ
    # ρ_c = K T e^{-rT} N(d2), ∂d2/∂σ = -d1/σ
    vera_c = K * T * dr * nd2 * (-d1 / sig)
    vera_p = -K * T * dr * nd2 * (d1 / sig)
    vera = np.where(is_call, vera_c, vera_p)

    speed = -gamma / S * (1.0 + d1 / (sig * sqrtT))
    zomma = gamma * (d1 * d2 - 1.0) / sig
    ultima = vega / (sig**2) * ((d1 * d2) ** 2 - d1 * d2 - d1**2 - d2**2)

    # color = ∂Γ/∂t = -∂Γ/∂T
    # Γ = e^{-qT} n(d1) / (S σ √T)
    dGamma_dT = gamma * (-qv - d1 * d1_dT - 1.0 / (2.0 * T))
    color = -dGamma_dT

    # np.where evaluates both branches, so a naive expression still divides by
    # zero and warns before the result is discarded. Guard the division, and
    # keep the sign: a zero-priced put tends to -inf, not +inf.
    elasticity = np.full(np.shape(px), np.nan, dtype=float)
    nz = px != 0.0
    np.divide(delta * S, px, out=elasticity, where=nz)
    elasticity = np.where(nz, elasticity, np.copysign(np.inf, delta))

    likes = (S,)

    def wrap(x: np.ndarray) -> Any:
        return _maybe_scalar(x, likes)

    return {
        "price": wrap(px),
        "delta": wrap(delta),
        "dual_delta": wrap(dual_delta),
        "vega": wrap(vega),
        "theta": wrap(theta),
        "rho": wrap(rho),
        "rho_dividend": wrap(rho_dividend),
        "gamma": wrap(gamma),
        "dual_gamma": wrap(dual_gamma),
        "vanna": wrap(vanna),
        "volga": wrap(volga),
        "charm": wrap(charm),
        "veta": wrap(veta),
        "vera": wrap(vera),
        "speed": wrap(speed),
        "zomma": wrap(zomma),
        "color": wrap(color),
        "ultima": wrap(ultima),
        "elasticity": wrap(elasticity),
    }


def greeks(
    S: ArrayLike,
    K: ArrayLike,
    T: ArrayLike,
    r: ArrayLike,
    sigma: ArrayLike,
    call_put: CallPut | ArrayLike = "call",
    *,
    q: ArrayLike | None = None,
    F: ArrayLike | None = None,
    conventions: GreeksConventions = DEFAULT_CONVENTIONS,
) -> GreeksCatalog:
    """Full named catalog with ``conventions`` applied."""
    return apply_conventions(
        raw_greeks(S, K, T, r, sigma, call_put, q=q, F=F),
        conventions,
    )
