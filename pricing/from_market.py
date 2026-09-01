"""Glue from marketdata Contract/Quote/Forward to pricing scalars.

Reads spot, strike, expiry, call/put, and (optionally) a parity forward.
Never reads vendor greeks or vendor implied volatility.
"""

from __future__ import annotations

from datetime import UTC, datetime

from marketdata.types import Contract, Forward, Quote
from pricing.bsm import CallPut
from pricing.conventions import DEFAULT_CONVENTIONS, GreeksCatalog, GreeksConventions
from pricing.engine import Engine, EuropeanBSM
from pricing.iv import implied_vol as invert_iv

_ENGINE = EuropeanBSM()


def year_fraction(contract: Contract, asof_ns: int, *, days: int = 365) -> float:
    """ACT/365 year fraction from as-of instant to expiry (UTC midnight)."""
    expiry = datetime(contract.expiry.year, contract.expiry.month, contract.expiry.day, tzinfo=UTC)
    expiry_ns = expiry.timestamp() * 1e9
    t = (expiry_ns - asof_ns) / (days * 86400.0 * 1e9)
    if t <= 0:
        raise ValueError(f"non-positive T: expiry={contract.expiry} asof_ns={asof_ns}")
    return t


def price_quote(
    quote: Quote,
    *,
    r: float,
    sigma: float,
    q: float | None = None,
    forward: Forward | None = None,
    engine: Engine | None = None,
) -> float:
    """Price using quote.underlying_price and contract fields — not vendor IV."""
    S, T, cp, F = _spot_t_cp(quote, forward)
    eng = engine or _ENGINE
    return float(eng.price(S, quote.contract.strike, T, r, sigma, cp, q=q, F=F))


def greeks_quote(
    quote: Quote,
    *,
    r: float,
    sigma: float,
    q: float | None = None,
    forward: Forward | None = None,
    engine: Engine | None = None,
    conventions: GreeksConventions = DEFAULT_CONVENTIONS,
) -> GreeksCatalog:
    """Greeks from market spot and our σ. Vendor greeks on the quote are ignored."""
    S, T, cp, F = _spot_t_cp(quote, forward)
    eng = engine or _ENGINE
    return eng.greeks(S, quote.contract.strike, T, r, sigma, cp, q=q, F=F, conventions=conventions)


def implied_vol_quote(
    quote: Quote,
    *,
    r: float,
    q: float | None = None,
    forward: Forward | None = None,
) -> float:
    """Invert the quote's last/close; ignore the vendor IV diagnostic."""
    px = quote.market_price
    if px is None:
        raise ValueError("quote has no last or day_close to invert")
    S, T, cp, F = _spot_t_cp(quote, forward)
    return invert_iv(px, S, quote.contract.strike, T, r, cp, q=q, F=F)


def _spot_t_cp(quote: Quote, forward: Forward | None) -> tuple[float, float, CallPut, float | None]:
    if quote.underlying_price is None:
        raise ValueError("quote has no underlying_price")
    if quote.asof_ns is None:
        raise ValueError("quote has no asof_ns")
    S = float(quote.underlying_price)
    T = year_fraction(quote.contract, quote.asof_ns)
    cp: CallPut = quote.contract.call_put
    F = None if forward is None else float(forward.forward)
    return S, T, cp, F
