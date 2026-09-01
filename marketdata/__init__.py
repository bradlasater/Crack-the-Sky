"""Typed reads of the clean parquet warehouse: OPRA contracts, catalog, as-of.

Vendor snapshot greeks and implied volatility are carried on :class:`Quote`
as diagnostics. They are never pricing inputs — see :mod:`pricing`.
"""

from __future__ import annotations

from marketdata.catalog import (
    ASOF_DATASETS,
    AsOfError,
    CatalogError,
    SchemaError,
    list_partitions,
    read_asof,
    read_partition,
)
from marketdata.opra import (
    ALLOWED_ROOTS,
    MULTIPLIER,
    OPRAParseError,
    format_opra,
    parse_opra,
    ticker_root,
)
from marketdata.types import (
    Contract,
    Forward,
    Quote,
    quotes_from_snapshot_rows,
    quotes_from_snapshot_table,
)
from marketdata.validate import Check, validate_table, validate_tickers

__all__ = [
    "ALLOWED_ROOTS",
    "ASOF_DATASETS",
    "MULTIPLIER",
    "AsOfError",
    "CatalogError",
    "Check",
    "Contract",
    "Forward",
    "OPRAParseError",
    "Quote",
    "SchemaError",
    "format_opra",
    "list_partitions",
    "parse_opra",
    "quotes_from_snapshot_rows",
    "quotes_from_snapshot_table",
    "read_asof",
    "read_partition",
    "ticker_root",
    "validate_table",
    "validate_tickers",
]
