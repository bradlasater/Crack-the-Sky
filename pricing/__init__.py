"""Calculators: European BSM, IV inversion, American CRR. Not a surface.

Vendor snapshot greeks / IV columns are never inputs here. Pass
``S, K, T, r, q`` (or ``F``) and ``sigma`` explicitly — invert ``sigma``
from a market price with ``pricing.iv.implied_vol`` when needed.

Import the submodules directly (``pricing.bsm``, ``pricing.iv``,
``pricing.engine``, ``pricing.conventions``, ``pricing.from_market``,
``pricing.drift_check``).
"""
