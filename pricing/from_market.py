"""Glue from marketdata Contract/Quote/Forward to pricing scalars.

Reads spot, strike, expiry, call/put, and (optionally) a parity forward.
Never reads vendor greeks or vendor implied volatility.

Two things here are load-bearing on this data feed:

* **Expiry is an instant.** Settlement is 16:00 ET for SPY/SPXW and 09:30 ET
  for AM-settled SPX (:data:`marketdata.opra.SETTLEMENT_ET`). Using the expiry
  *date* at UTC midnight is 20:00 ET the day before, understating T at every
  tenor and biasing inverted IV by ~108bp at 7 DTE.
* **SPX has no spot.** The index level is not entitled on this tier and the
  snapshot carries ``underlying_price = null`` for the whole SPX chain (~68% of
  the universe; SPXW alone is ~98% of SPX trade volume). Pass the per-expiry
  parity ``Forward`` and pricing switches to Black-76, which is the right model
  for a European index option anyway -- no dividend yield to guess.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from marketdata.opra import settlement_time_et
from marketdata.types import Contract, Forward, Quote
from pricing.bsm import CallPut
from pricing.conventions import DEFAULT_CONVENTIONS, GreeksCatalog, GreeksConventions
from pricing.engine import Engine, EuropeanBSM
from pricing.iv import implied_vol as invert_iv

ET = ZoneInfo("America/New_York")
_ENGINE = EuropeanBSM()


def expiry_instant(contract: Contract) -> datetime:
    """The moment ``contract`` settles, as an aware UTC datetime.

    16:00 ET for SPY and SPXW; 09:30 ET for AM-settled SPX.
    """
    hour, minute = settlement_time_et(contract.root)
    local = datetime(
        contract.expiry.year, contract.expiry.month, contract.expiry.day,
        hour, minute, tzinfo=ET,
    )
    return local.astimezone(UTC)


def year_fraction(contract: Contract, asof_ns: int, *, days: int = 365) -> float:
    """ACT/365 year fraction from the as-of instant to the settlement instant."""
    expiry_ns = expiry_instant(contract).timestamp() * 1e9
    t = (expiry_ns - asof_ns) / (days * 86400.0 * 1e9)
    if t <= 0:
        raise ValueError(
            f"non-positive T: {contract.ticker or contract.root} settles "
            f"{expiry_instant(contract).isoformat()}, asof_ns={asof_ns}"
        )
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
    S, T, cp, F = _spot_t_cp(quote, forward, r)
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
    S, T, cp, F = _spot_t_cp(quote, forward, r)
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
    S, T, cp, F = _spot_t_cp(quote, forward, r)
    return invert_iv(px, S, quote.contract.strike, T, r, cp, q=q, F=F)


def _spot_t_cp(
    quote: Quote, forward: Forward | None, r: float
) -> tuple[float, float, CallPut, float | None]:
    """Resolve ``(S, T, call_put, F)`` for one quote.

    When the snapshot carries no ``underlying_price`` -- which is the entire
    SPX chain on this tier -- fall back to the parity forward and price in
    Black-76 terms: ``S = F e^{-rT}`` with ``q = 0`` makes
    ``d1 = (ln(F/K) + sigma^2 T/2)/(sigma sqrt(T))`` and
    ``price = e^{-rT}[F N(d1) - K N(d2)]`` fall out of the existing BSM code
    with no new maths. Delta is then with respect to that synthetic spot.
    """
    if quote.asof_ns is None:
        raise ValueError("quote has no asof_ns")
    T = year_fraction(quote.contract, quote.asof_ns)
    cp: CallPut = quote.contract.call_put

    if quote.underlying_price is not None:
        S = float(quote.underlying_price)
        return S, T, cp, (None if forward is None else float(forward.forward))

    if forward is None:
        raise ValueError(
            f"{quote.contract.ticker or quote.contract.root}: snapshot has no "
            "underlying_price (expected for SPX -- the index level is not "
            "entitled on this tier) and no parity forward was supplied; pass "
            "the matching forwards row"
        )
    if forward.expiry != quote.contract.expiry:
        raise ValueError(
            f"forward expiry {forward.expiry} does not match contract expiry "
            f"{quote.contract.expiry}"
        )
    # Black-76 via the synthetic spot; q resolves to 0 inside resolve_q.
    return float(forward.forward) * math.exp(-r * T), T, cp, None
