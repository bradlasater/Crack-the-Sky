"""Pricing engines: analytic European BSM and American CRR bump-and-revalue.

SPY is American; SPX/SPXW are European. The identity and finite-difference
suites run against :class:`EuropeanBSM` so American early-exercise value
cannot contaminate them.
"""

from __future__ import annotations

import math
from typing import Any, Protocol, runtime_checkable

import numpy as np

from pricing.bsm import CallPut, resolve_q
from pricing.bsm import greeks as bsm_greeks
from pricing.bsm import price as bsm_price
from pricing.conventions import (
    DEFAULT_CONVENTIONS,
    GreeksCatalog,
    GreeksConventions,
    apply_conventions,
)

ArrayLike = Any


@runtime_checkable
class Engine(Protocol):
    """Common Greek *names* for European analytic and American tree engines."""

    name: str

    def price(
        self,
        S: ArrayLike,
        K: ArrayLike,
        T: ArrayLike,
        r: ArrayLike,
        sigma: ArrayLike,
        call_put: CallPut | ArrayLike = "call",
        *,
        q: ArrayLike | None = None,
        F: ArrayLike | None = None,
    ) -> Any: ...

    def greeks(
        self,
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
    ) -> GreeksCatalog: ...


class EuropeanBSM:
    """Closed-form BSM. Implements :class:`Engine`."""

    name = "european_bsm"

    def price(
        self,
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
        return bsm_price(S, K, T, r, sigma, call_put, q=q, F=F)

    def greeks(
        self,
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
        return bsm_greeks(S, K, T, r, sigma, call_put, q=q, F=F, conventions=conventions)


_CALL_PUT = {"call": True, "c": True, "put": False, "p": False}


def _is_call(call_put: Any) -> bool:
    """Strict call/put normalizer shared by every CRR entry point.

    ``str(x).lower().startswith("c")`` silently read "invalid" as a put and
    priced it, which differs from the BSM API and turns a typo into a
    plausible number.
    """
    key = str(call_put).strip().lower()
    if key not in _CALL_PUT:
        raise ValueError(f"call_put must be call/put, got {call_put!r}")
    return _CALL_PUT[key]


def crr_price(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    call_put: CallPut = "call",
    *,
    q: float = 0.0,
    n_steps: int = 401,
    american: bool = True,
) -> float:
    """Cox–Ross–Rubinstein binomial price (scalar)."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        raise ValueError("S, K, T, sigma must all be strictly positive")
    n = int(n_steps)
    if n < 2:
        raise ValueError("n_steps must be >= 2")
    is_call = _is_call(call_put)
    dt = T / n
    u = float(np.exp(sigma * np.sqrt(dt)))
    d = 1.0 / u
    a = float(np.exp((r - q) * dt))
    denom = u - d
    if abs(denom) < 1e-18:
        raise ValueError("CRR u-d underflow")
    p = (a - d) / denom
    if not (0.0 <= p <= 1.0):
        # A CRR tree is only arbitrage-free while d < e^{(r-q)dt} < u. Clipping
        # p silently changes the expected growth rate and returns a
        # plausible-looking but wrong price, which is worse than no price.
        # More steps shrink dt and usually restore the condition.
        raise ValueError(
            f"CRR risk-neutral probability {p:.6g} outside [0, 1]: the tree is "
            f"not arbitrage-free at n_steps={n} (needs d < exp((r-q)dt) < u, "
            f"got d={d:.6g}, exp((r-q)dt)={a:.6g}, u={u:.6g}). "
            "Increase n_steps, or check sigma/r/q."
        )
    disc = float(np.exp(-r * dt))
    j = np.arange(n + 1, dtype=float)
    spot = S * u**j * d ** (n - j)
    value = np.maximum(spot - K, 0.0) if is_call else np.maximum(K - spot, 0.0)
    for i in range(n - 1, -1, -1):
        value = disc * (p * value[1:] + (1.0 - p) * value[:-1])
        if american:
            j = np.arange(i + 1, dtype=float)
            spot_i = S * u**j * d ** (i - j)
            value = np.maximum(value, spot_i - K) if is_call else np.maximum(value, K - spot_i)
    return float(value[0])


class AmericanCRR:
    """American CRR tree; Greeks by bump-and-revalue of the same names."""

    name = "american_crr"

    def __init__(self, n_steps: int = 401) -> None:
        self.n_steps = int(n_steps)

    def price(
        self,
        S: ArrayLike,
        K: ArrayLike,
        T: ArrayLike,
        r: ArrayLike,
        sigma: ArrayLike,
        call_put: CallPut | ArrayLike = "call",
        *,
        q: ArrayLike | None = None,
        F: ArrayLike | None = None,
    ) -> float:
        S_ = float(np.asarray(S, dtype=float))
        K_ = float(np.asarray(K, dtype=float))
        T_ = float(np.asarray(T, dtype=float))
        r_ = float(np.asarray(r, dtype=float))
        sig = float(np.asarray(sigma, dtype=float))
        qv = float(resolve_q(S_, T_, r_, q=q, F=F))
        cp: CallPut = "call" if _is_call(call_put) else "put"
        return crr_price(S_, K_, T_, r_, sig, cp, q=qv, n_steps=self.n_steps, american=True)

    def greeks(
        self,
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
        S_ = float(np.asarray(S, dtype=float))
        K_ = float(np.asarray(K, dtype=float))
        T_ = float(np.asarray(T, dtype=float))
        r_ = float(np.asarray(r, dtype=float))
        sig = float(np.asarray(sigma, dtype=float))
        qv = float(resolve_q(S_, T_, r_, q=q, F=F))
        cp: CallPut = "call" if _is_call(call_put) else "put"
        raw = _bump_greeks(S_, K_, T_, r_, sig, qv, cp, self.n_steps)
        return apply_conventions(raw, conventions)


def _bump_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    q: float,
    call_put: CallPut,
    n_steps: int,
) -> dict[str, float]:
    """Raw (per 1.00, per year, spot) Greeks via CRR bump-and-revalue."""

    def v(
        S_=S,
        K_=K,
        T_=T,
        r_=r,
        sig=sigma,
        q_=q,
    ) -> float:
        return crr_price(S_, K_, T_, r_, sig, call_put, q=q_, n_steps=n_steps, american=True)

    px = v()
    hS = max(1e-4 * S, 1e-6)
    hK = max(1e-4 * K, 1e-6)
    hs = max(1e-4 * sigma, 1e-6)
    hr = 1e-5
    hq = 1e-5
    hT = max(min(1e-4 * T, T * 0.05), 1e-8)

    vp, vm = v(S_=S + hS), v(S_=S - hS)
    delta = (vp - vm) / (2.0 * hS)
    gamma = (vp - 2.0 * px + vm) / (hS**2)

    kp, km = v(K_=K + hK), v(K_=K - hK)
    dual_delta = (kp - km) / (2.0 * hK)
    dual_gamma = (kp - 2.0 * px + km) / (hK**2)

    sp, sm = v(sig=sigma + hs), v(sig=sigma - hs)
    vega = (sp - sm) / (2.0 * hs)
    volga = (sp - 2.0 * px + sm) / (hs**2)

    vanna = (
        v(S_=S + hS, sig=sigma + hs)
        - v(S_=S + hS, sig=sigma - hs)
        - v(S_=S - hS, sig=sigma + hs)
        + v(S_=S - hS, sig=sigma - hs)
    ) / (4.0 * hS * hs)

    t_minus = T - hT
    if t_minus <= 0:
        t_minus = T * 0.5
        hT = T - t_minus
    theta = (v(T_=t_minus) - px) / hT

    rho = (v(r_=r + hr) - v(r_=r - hr)) / (2.0 * hr)
    rho_dividend = (v(q_=q + hq) - v(q_=q - hq)) / (2.0 * hq)

    def delta_at(**kw: float) -> float:
        args = {"S_": S, "K_": K, "T_": T, "r_": r, "sig": sigma, "q_": q}
        args.update(kw)
        return (v(**{**args, "S_": args["S_"] + hS}) - v(**{**args, "S_": args["S_"] - hS})) / (
            2.0 * hS
        )

    def gamma_at(**kw: float) -> float:
        args = {"S_": S, "K_": K, "T_": T, "r_": r, "sig": sigma, "q_": q}
        args.update(kw)
        s0 = args["S_"]
        return (v(**{**args, "S_": s0 + hS}) - 2.0 * v(**args) + v(**{**args, "S_": s0 - hS})) / (
            hS**2
        )

    def vega_at(**kw: float) -> float:
        args = {"S_": S, "K_": K, "T_": T, "r_": r, "sig": sigma, "q_": q}
        args.update(kw)
        sig0 = args["sig"]
        return (v(**{**args, "sig": sig0 + hs}) - v(**{**args, "sig": sig0 - hs})) / (2.0 * hs)

    def rho_at(sig: float) -> float:
        return (v(sig=sig, r_=r + hr) - v(sig=sig, r_=r - hr)) / (2.0 * hr)

    charm = (delta_at(T_=t_minus) - delta) / hT
    veta = (vega_at(T_=t_minus) - vega) / hT
    color = (gamma_at(T_=t_minus) - gamma) / hT
    speed = (gamma_at(S_=S + hS) - gamma_at(S_=S - hS)) / (2.0 * hS)
    zomma = (gamma_at(sig=sigma + hs) - gamma_at(sig=sigma - hs)) / (2.0 * hs)
    vera = (rho_at(sigma + hs) - rho_at(sigma - hs)) / (2.0 * hs)
    ultima = (vega_at(sig=sigma + hs) - 2.0 * vega + vega_at(sig=sigma - hs)) / (hs**2)
    # Keep the sign at a zero price, as bsm.raw_greeks does: a zero-priced
    # put tends to -inf, not +inf. A bumped delta of exactly 0.0 carries no
    # sign (deep-OTM: center and both bumps all price to 0.0), so fall back
    # to the option side.
    if px == 0.0:
        sign = delta if delta != 0.0 else (-1.0 if call_put == "put" else 1.0)
        elasticity = math.copysign(float("inf"), sign)
    else:
        elasticity = delta * S / px

    return {
        "price": px,
        "delta": delta,
        "dual_delta": dual_delta,
        "vega": vega,
        "theta": theta,
        "rho": rho,
        "rho_dividend": rho_dividend,
        "gamma": gamma,
        "dual_gamma": dual_gamma,
        "vanna": vanna,
        "volga": volga,
        "charm": charm,
        "veta": veta,
        "vera": vera,
        "speed": speed,
        "zomma": zomma,
        "color": color,
        "ultima": ultima,
        "elasticity": elasticity,
    }
