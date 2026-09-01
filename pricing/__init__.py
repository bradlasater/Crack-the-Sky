"""Calculators: European BSM, IV inversion, American CRR. Not a surface.

Vendor snapshot greeks / IV columns are never inputs here. Pass
``S, K, T, r, q`` (or ``F``) and ``sigma`` explicitly — invert ``sigma``
from a market price with :func:`implied_vol` when needed.
"""

from __future__ import annotations

from pricing.bsm import d1_d2, greeks, price, raw_greeks, resolve_q
from pricing.conventions import (
    DEFAULT_CONVENTIONS,
    GREEK_NAMES,
    GreeksCatalog,
    GreeksConventions,
)
from pricing.engine import AmericanCRR, Engine, EuropeanBSM, crr_price
from pricing.from_market import greeks_quote, implied_vol_quote, price_quote
from pricing.iv import discounted_bounds, implied_vol

__all__ = [
    "DEFAULT_CONVENTIONS",
    "GREEK_NAMES",
    "AmericanCRR",
    "Engine",
    "EuropeanBSM",
    "GreeksCatalog",
    "GreeksConventions",
    "crr_price",
    "d1_d2",
    "discounted_bounds",
    "greeks",
    "greeks_quote",
    "implied_vol",
    "implied_vol_quote",
    "price",
    "price_quote",
    "raw_greeks",
    "resolve_q",
]
