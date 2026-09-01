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
Greeks columns are kept nullable, but they ARE populated on this tier for
any contract the vendor can price: measured 2026-08-31, 12,725 of 13,514 SPY
snapshot rows carried non-null ``implied_volatility`` and ``greeks_delta``,
and ``open_interest`` was non-null on all 13,514. Nulls appear on contracts
with no usable market (deep ITM, expiring), not as a tier limitation.
``option_snapshots`` is the only dataset here that cannot be backfilled from
flat files -- it is gone if it is not captured live.

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


def _build_schemas() -> dict[str, Any]:
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

    # Daily OHLCV for equity/ETF underlyings, from the grouped-daily endpoint.
    # One REST call returns the whole US equity market for a date, so this is
    # the cheapest independent cross-check we have on SPY.
    underlying_day_bar_fields = [
        pa.field("ticker", pa.string()),
        pa.field("start_ms", pa.int64()),   # aggs 't' = ms epoch, as delivered
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

    # Per-expiry forward recovered from put-call parity on the option chain.
    # The index level (I:SPX) is NOT entitled on this tier at any endpoint, so
    # parity on the chain we already sweep is the only way to obtain an SPX
    # reference price. F = K + C - P at the strike minimising |C - P|.
    forward_fields = [
        pa.field("underlying_ticker", pa.string()),
        pa.field("expiration_date", pa.string()),
        pa.field("atm_strike", pa.float64()),
        pa.field("forward", pa.float64()),
        pa.field("call_price", pa.float64()),
        pa.field("put_price", pa.float64()),
        pa.field("pairs", pa.int64()),        # call/put pairs available
        pa.field("asof_ns", pa.int64()),
        pa.field("method", pa.string()),      # 'parity' | 'spot' | 'proxy'
    ]

    contracts_schema = pa.schema(contract_fields)
    return {
        "forwards": pa.schema(forward_fields),
        "contracts": contracts_schema,
        "contracts_expired": contracts_schema,  # same schema as contracts
        "option_snapshots": pa.schema(snapshot_fields),
        "option_minute_bars": pa.schema(option_bar_fields),
        "option_day_bars": pa.schema(option_bar_fields),
        "option_trades": pa.schema(trade_fields),
        "underlying_minute_bars": pa.schema(underlying_bar_fields),
        "underlying_day_bars": pa.schema(underlying_day_bar_fields),
        "dividends": pa.schema(dividend_fields),
        "splits": pa.schema(split_fields),
    }


# Empty when pyarrow is unavailable; landing.write_clean fails loudly instead.
SCHEMAS: dict[str, Any] = _build_schemas() if pa is not None else {}


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
    "flatten_snapshot",
    "contract_record",
    "pa",
]
