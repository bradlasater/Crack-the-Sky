"""PyArrow schemas and ClickHouse DDL for every landed dataset.

Conventions (per SPEC):
  * ticker / contract fields      -> ``pa.string()``
  * epoch timestamps              -> ``pa.int64()``, name suffix ``_ns``/``_ms``
    (timestamps are stored exactly as delivered; never converted)
  * prices                        -> ``pa.float64()``
  * sizes / open interest         -> ``pa.int64()``
  * ``option_{minute_bars,day_bars,trades}`` carry a ``src`` column with
    values ``'ws'`` | ``'rest'`` | ``'flatfile'``.

``option_snapshots`` flattens the nested snapshot payload with
``details_`` / ``day_`` / ``last_trade_`` / ``underlying_`` prefixes; use
:func:`flatten_snapshot` to convert a raw API result into a schema record.
Greeks columns are kept nullable: the current tier delivers ``greeks`` as an
empty object and omits ``implied_volatility``, but we capture them when present.

PyArrow is import-guarded: this module imports cleanly without pyarrow so
raw-only paths keep working; only :func:`ingest.common.landing.write_clean`
hard-requires it.
"""

from __future__ import annotations

import json
from typing import Any

try:  # import-guarded: schemas must be importable without pyarrow installed
    import pyarrow as pa
except ImportError:  # pragma: no cover - exercised only on pyarrow-less hosts
    pa = None  # type: ignore[assignment]


def _build_schemas() -> "dict[str, Any]":
    """Construct the SCHEMAS dict; requires pyarrow."""
    if pa is None:  # pragma: no cover
        raise ImportError(
            "pyarrow is required to build dataset schemas; "
            "install it (pip install -r requirements.txt) before writing clean data"
        )

    contract_fields = [
        pa.field("ticker", pa.string()),
        pa.field("underlying_ticker", pa.string()),
        pa.field("contract_type", pa.string()),
        pa.field("exercise_style", pa.string()),
        pa.field("expiration_date", pa.string()),
        pa.field("strike_price", pa.float64()),
        pa.field("shares_per_contract", pa.int64()),
        pa.field("primary_exchange", pa.string()),
        pa.field("cfi", pa.string()),
        # nested list payload from the API, stored as a JSON-encoded string
        pa.field("additional_underlyings", pa.string()),
    ]

    snapshot_fields = [
        # details{} flattened
        pa.field("ticker", pa.string()),
        pa.field("details_contract_type", pa.string()),
        pa.field("details_exercise_style", pa.string()),
        pa.field("details_expiration_date", pa.string()),
        pa.field("details_strike_price", pa.float64()),
        pa.field("details_shares_per_contract", pa.int64()),
        # day{} flattened
        pa.field("day_open", pa.float64()),
        pa.field("day_high", pa.float64()),
        pa.field("day_low", pa.float64()),
        pa.field("day_close", pa.float64()),
        pa.field("day_volume", pa.float64()),
        pa.field("day_vwap", pa.float64()),
        pa.field("day_last_updated_ns", pa.int64()),
        # last_trade{} flattened
        pa.field("last_trade_price", pa.float64()),
        pa.field("last_trade_size", pa.int64()),
        pa.field("last_trade_exchange", pa.int64()),
        pa.field("last_trade_conditions", pa.string()),  # JSON-encoded list
        pa.field("last_trade_sip_timestamp_ns", pa.int64()),
        pa.field("last_trade_timeframe", pa.string()),
        # top-level snapshot scalars
        pa.field("open_interest", pa.int64()),
        pa.field("break_even_price", pa.float64()),
        # underlying_asset{} flattened
        pa.field("underlying_ticker", pa.string()),
        pa.field("underlying_price", pa.float64()),
        pa.field("underlying_timeframe", pa.string()),
        pa.field("underlying_last_updated_ns", pa.int64()),
        # greeks are delivered as {} on this tier; keep columns nullable
        pa.field("greeks_delta", pa.float64()),
        pa.field("greeks_gamma", pa.float64()),
        pa.field("greeks_theta", pa.float64()),
        pa.field("greeks_vega", pa.float64()),
        pa.field("implied_volatility", pa.float64()),
    ]

    # Shared by option_minute_bars / option_day_bars. Sources: WS AM events
    # (s/e = window start/end ns, op/vw/z), REST aggs, flat-file aggs
    # (window_start ns UTC, transactions).
    option_bar_fields = [
        pa.field("ticker", pa.string()),
        pa.field("window_start_ns", pa.int64()),
        pa.field("window_end_ns", pa.int64()),
        pa.field("open", pa.float64()),
        pa.field("high", pa.float64()),
        pa.field("low", pa.float64()),
        pa.field("close", pa.float64()),
        pa.field("volume", pa.float64()),
        pa.field("vwap", pa.float64()),
        pa.field("transactions", pa.int64()),
        pa.field("official_open", pa.float64()),  # WS 'op' only
        pa.field("accumulated_volume", pa.float64()),  # WS 'av' only
        pa.field("src", pa.string()),  # 'ws' | 'rest' | 'flatfile'
    ]

    trade_fields = [
        pa.field("ticker", pa.string()),
        pa.field("price", pa.float64()),
        pa.field("size", pa.int64()),
        pa.field("exchange", pa.int64()),
        pa.field("conditions", pa.string()),  # JSON-encoded list
        pa.field("correction", pa.int64()),
        pa.field("trade_id", pa.string()),
        pa.field("sequence_number", pa.int64()),
        pa.field("sip_timestamp_ns", pa.int64()),
        # absent from delayed REST trades; present in flat files
        pa.field("participant_timestamp_ns", pa.int64()),
        pa.field("src", pa.string()),  # 'ws' | 'rest' | 'flatfile'
    ]

    underlying_bar_fields = [
        pa.field("ticker", pa.string()),
        pa.field("start_ms", pa.int64()),  # REST aggs 't' = ms epoch, as delivered
        pa.field("open", pa.float64()),
        pa.field("high", pa.float64()),
        pa.field("low", pa.float64()),
        pa.field("close", pa.float64()),
        pa.field("volume", pa.float64()),
        pa.field("vwap", pa.float64()),
        pa.field("transactions", pa.int64()),
    ]

    dividend_fields = [
        pa.field("ticker", pa.string()),
        pa.field("dividend_id", pa.string()),
        pa.field("cash_amount", pa.float64()),
        pa.field("currency", pa.string()),
        pa.field("dividend_type", pa.string()),
        pa.field("frequency", pa.int64()),
        pa.field("declaration_date", pa.string()),
        pa.field("ex_dividend_date", pa.string()),
        pa.field("record_date", pa.string()),
        pa.field("pay_date", pa.string()),
    ]

    split_fields = [
        pa.field("ticker", pa.string()),
        pa.field("split_id", pa.string()),
        pa.field("execution_date", pa.string()),
        pa.field("split_from", pa.float64()),
        pa.field("split_to", pa.float64()),
    ]

    contracts_schema = pa.schema(contract_fields)
    return {
        "contracts": contracts_schema,
        "contracts_expired": contracts_schema,  # same schema as contracts
        "option_snapshots": pa.schema(snapshot_fields),
        "option_minute_bars": pa.schema(option_bar_fields),
        "option_day_bars": pa.schema(option_bar_fields),
        "option_trades": pa.schema(trade_fields),
        "underlying_minute_bars": pa.schema(underlying_bar_fields),
        "dividends": pa.schema(dividend_fields),
        "splits": pa.schema(split_fields),
    }


# Empty when pyarrow is unavailable; landing.write_clean fails loudly instead.
SCHEMAS: "dict[str, Any]" = _build_schemas() if pa is not None else {}


# ---------------------------------------------------------------------------
# ClickHouse DDL (strings, for later provisioning use)
# ---------------------------------------------------------------------------

def _ddl(dataset: str, columns: list[tuple[str, str]], order_by: str) -> str:
    cols = ",\n    ".join(f"{name} {typ}" for name, typ in columns)
    return (
        f"CREATE TABLE IF NOT EXISTS massive.{dataset} (\n"
        f"    {cols}\n"
        f") ENGINE = ReplacingMergeTree\n"
        f"ORDER BY ({order_by});"
    )


def _build_ddl() -> dict[str, str]:
    contracts_cols = [
        ("ticker", "LowCardinality(String)"),
        ("underlying_ticker", "LowCardinality(String)"),
        ("contract_type", "LowCardinality(String)"),
        ("exercise_style", "LowCardinality(String)"),
        ("expiration_date", "Date"),
        ("strike_price", "Float64"),
        ("shares_per_contract", "Int64"),
        ("primary_exchange", "LowCardinality(String)"),
        ("cfi", "String"),
        ("additional_underlyings", "String"),
    ]
    contracts_ddl = _ddl("contracts", contracts_cols, "underlying_ticker, ticker")

    snapshot_cols = [
        ("ticker", "LowCardinality(String)"),
        ("details_contract_type", "LowCardinality(String)"),
        ("details_exercise_style", "LowCardinality(String)"),
        ("details_expiration_date", "Date"),
        ("details_strike_price", "Float64"),
        ("details_shares_per_contract", "Int64"),
        ("day_open", "Float64"),
        ("day_high", "Float64"),
        ("day_low", "Float64"),
        ("day_close", "Float64"),
        ("day_volume", "Float64"),
        ("day_vwap", "Float64"),
        ("day_last_updated_ns", "DateTime64(9, 'UTC')"),
        ("last_trade_price", "Float64"),
        ("last_trade_size", "Int64"),
        ("last_trade_exchange", "Int64"),
        ("last_trade_conditions", "String"),
        ("last_trade_sip_timestamp_ns", "DateTime64(9, 'UTC')"),
        ("last_trade_timeframe", "LowCardinality(String)"),
        ("open_interest", "Int64"),
        ("break_even_price", "Float64"),
        ("underlying_ticker", "LowCardinality(String)"),
        ("underlying_price", "Float64"),
        ("underlying_timeframe", "LowCardinality(String)"),
        ("underlying_last_updated_ns", "DateTime64(9, 'UTC')"),
        ("greeks_delta", "Nullable(Float64)"),
        ("greeks_gamma", "Nullable(Float64)"),
        ("greeks_theta", "Nullable(Float64)"),
        ("greeks_vega", "Nullable(Float64)"),
        ("implied_volatility", "Nullable(Float64)"),
    ]

    option_bar_cols = [
        ("ticker", "LowCardinality(String)"),
        ("window_start_ns", "DateTime64(9, 'UTC')"),
        ("window_end_ns", "DateTime64(9, 'UTC')"),
        ("open", "Float64"),
        ("high", "Float64"),
        ("low", "Float64"),
        ("close", "Float64"),
        ("volume", "Float64"),
        ("vwap", "Float64"),
        ("transactions", "Int64"),
        ("official_open", "Nullable(Float64)"),
        ("accumulated_volume", "Nullable(Float64)"),
        ("src", "LowCardinality(String)"),
    ]

    trade_cols = [
        ("ticker", "LowCardinality(String)"),
        ("price", "Float64"),
        ("size", "Int64"),
        ("exchange", "Int64"),
        ("conditions", "String"),
        ("correction", "Nullable(Int64)"),
        ("trade_id", "String"),
        ("sequence_number", "Int64"),
        ("sip_timestamp_ns", "DateTime64(9, 'UTC')"),
        ("participant_timestamp_ns", "Nullable(DateTime64(9, 'UTC'))"),
        ("src", "LowCardinality(String)"),
    ]

    underlying_bar_cols = [
        ("ticker", "LowCardinality(String)"),
        ("start_ms", "DateTime64(3, 'UTC')"),
        ("open", "Float64"),
        ("high", "Float64"),
        ("low", "Float64"),
        ("close", "Float64"),
        ("volume", "Float64"),
        ("vwap", "Float64"),
        ("transactions", "Int64"),
    ]

    dividend_cols = [
        ("ticker", "LowCardinality(String)"),
        ("dividend_id", "String"),
        ("cash_amount", "Float64"),
        ("currency", "LowCardinality(String)"),
        ("dividend_type", "LowCardinality(String)"),
        ("frequency", "Int64"),
        ("declaration_date", "Nullable(Date)"),
        ("ex_dividend_date", "Date"),
        ("record_date", "Nullable(Date)"),
        ("pay_date", "Nullable(Date)"),
    ]

    split_cols = [
        ("ticker", "LowCardinality(String)"),
        ("split_id", "String"),
        ("execution_date", "Date"),
        ("split_from", "Float64"),
        ("split_to", "Float64"),
    ]

    return {
        "contracts": contracts_ddl,
        "contracts_expired": _ddl(
            "contracts_expired", contracts_cols, "underlying_ticker, ticker"
        ),
        "option_snapshots": _ddl("option_snapshots", snapshot_cols, "ticker"),
        "option_minute_bars": _ddl(
            "option_minute_bars", option_bar_cols, "ticker, window_start_ns"
        ),
        "option_day_bars": _ddl(
            "option_day_bars", option_bar_cols, "ticker, window_start_ns"
        ),
        "option_trades": _ddl(
            "option_trades", trade_cols, "ticker, sip_timestamp_ns"
        ),
        "underlying_minute_bars": _ddl(
            "underlying_minute_bars", underlying_bar_cols, "ticker, start_ms"
        ),
        "dividends": _ddl("dividends", dividend_cols, "ticker, ex_dividend_date"),
        "splits": _ddl("splits", split_cols, "ticker, execution_date"),
    }


CLICKHOUSE_DDL: dict[str, str] = _build_ddl()


# ---------------------------------------------------------------------------
# Mapping helpers (fixture -> schema record); shared by jobs and tests.
# ---------------------------------------------------------------------------

def flatten_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    """Flatten one ``/v3/snapshot/options`` result into an option_snapshots record.

    Nested ``details`` / ``day`` / ``last_trade`` / ``underlying_asset``
    objects are promoted with their respective prefixes. ``conditions`` lists
    are JSON-encoded; missing/absent values become ``None`` (nullable).
    """
    details = result.get("details") or {}
    day = result.get("day") or {}
    last_trade = result.get("last_trade") or {}
    underlying = result.get("underlying_asset") or {}
    greeks = result.get("greeks") or {}
    conditions = last_trade.get("conditions")
    return {
        "ticker": details.get("ticker"),
        "details_contract_type": details.get("contract_type"),
        "details_exercise_style": details.get("exercise_style"),
        "details_expiration_date": details.get("expiration_date"),
        "details_strike_price": details.get("strike_price"),
        "details_shares_per_contract": details.get("shares_per_contract"),
        "day_open": day.get("open"),
        "day_high": day.get("high"),
        "day_low": day.get("low"),
        "day_close": day.get("close"),
        "day_volume": day.get("volume"),
        "day_vwap": day.get("vwap"),
        "day_last_updated_ns": day.get("last_updated"),
        "last_trade_price": last_trade.get("price"),
        "last_trade_size": last_trade.get("size"),
        "last_trade_exchange": last_trade.get("exchange"),
        "last_trade_conditions": (
            json.dumps(conditions) if conditions is not None else None
        ),
        "last_trade_sip_timestamp_ns": last_trade.get("sip_timestamp"),
        "last_trade_timeframe": last_trade.get("timeframe"),
        "open_interest": result.get("open_interest"),
        "break_even_price": result.get("break_even_price"),
        "underlying_ticker": underlying.get("ticker"),
        "underlying_price": underlying.get("price"),
        "underlying_timeframe": underlying.get("timeframe"),
        "underlying_last_updated_ns": underlying.get("last_updated"),
        "greeks_delta": greeks.get("delta"),
        "greeks_gamma": greeks.get("gamma"),
        "greeks_theta": greeks.get("theta"),
        "greeks_vega": greeks.get("vega"),
        "implied_volatility": result.get("implied_volatility"),
    }


def contract_record(result: dict[str, Any]) -> dict[str, Any]:
    """Map one ``/v3/reference/options/contracts`` result to a contracts record.

    ``additional_underlyings`` (a nested list) is JSON-encoded into a string.
    """
    additional = result.get("additional_underlyings")
    return {
        "ticker": result.get("ticker"),
        "underlying_ticker": result.get("underlying_ticker"),
        "contract_type": result.get("contract_type"),
        "exercise_style": result.get("exercise_style"),
        "expiration_date": result.get("expiration_date"),
        "strike_price": result.get("strike_price"),
        "shares_per_contract": result.get("shares_per_contract"),
        "primary_exchange": result.get("primary_exchange"),
        "cfi": result.get("cfi"),
        "additional_underlyings": (
            json.dumps(additional) if additional is not None else None
        ),
    }


__all__ = [
    "SCHEMAS",
    "CLICKHOUSE_DDL",
    "flatten_snapshot",
    "contract_record",
    "pa",
]
