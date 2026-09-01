"""Typed reads of the clean parquet warehouse: OPRA contracts, catalog, as-of.

Vendor snapshot greeks and implied volatility are carried on ``Quote``
as diagnostics. They are never pricing inputs — see ``pricing``.

Import the submodules directly (``marketdata.opra``, ``marketdata.types``,
``marketdata.catalog``, ``marketdata.validate``).
"""
