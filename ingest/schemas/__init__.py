"""PyArrow schemas for every landed dataset.

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
raw-only paths keep working; :func:`ingest.common.landing.write_clean` and
``marketdata.catalog`` (the partition readers) hard-require it.
"""

from __future__ import annotations

import json
from datetime import date
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

    # US Treasury par yield curve, from /fed/v1/treasury-yields. History goes
    # back to 1962-01-02. This is the discount curve every IV inversion needs;
    # before it existed, r was hardcoded to 0.04 everywhere.
    treasury_yield_fields = [
        pa.field("date", pa.string()),
        pa.field("yield_1_month", pa.float64()),
        pa.field("yield_3_month", pa.float64()),
        pa.field("yield_6_month", pa.float64()),
        pa.field("yield_1_year", pa.float64()),
        pa.field("yield_2_year", pa.float64()),
        pa.field("yield_3_year", pa.float64()),
        pa.field("yield_5_year", pa.float64()),
        pa.field("yield_7_year", pa.float64()),
        pa.field("yield_10_year", pa.float64()),
        pa.field("yield_20_year", pa.float64()),
        pa.field("yield_30_year", pa.float64()),
    ]

    # /fed/v1/inflation: CPI and PCE levels (indices, not rates).
    inflation_fields = [
        pa.field("date", pa.string()),
        pa.field("cpi", pa.float64()),
        pa.field("cpi_core", pa.float64()),
        pa.field("pce", pa.float64()),
        pa.field("pce_core", pa.float64()),
        pa.field("pce_spending", pa.float64()),
    ]

    # IBKR Flex trade rows. Deliberately close to the Flex field names so a
    # row can be traced back to the source XML without a mapping table.
    # Money is float64 like every other price here; quantity is signed by the
    # buy/sell side as IBKR reports it.
    ibkr_execution_fields = [
        pa.field("account_id", pa.string()),
        pa.field("trade_id", pa.string()),
        pa.field("exec_id", pa.string()),
        pa.field("order_id", pa.string()),
        pa.field("symbol", pa.string()),           # IBKR local symbol
        pa.field("opra_ticker", pa.string()),      # O:... when reconstructable
        pa.field("underlying_symbol", pa.string()),
        pa.field("asset_class", pa.string()),      # OPT / FOP / STK / FUT
        pa.field("put_call", pa.string()),
        pa.field("strike", pa.float64()),
        pa.field("expiry", pa.string()),
        pa.field("multiplier", pa.float64()),
        pa.field("buy_sell", pa.string()),
        pa.field("quantity", pa.float64()),
        pa.field("trade_price", pa.float64()),
        pa.field("trade_money", pa.float64()),
        pa.field("proceeds", pa.float64()),
        pa.field("commission", pa.float64()),
        pa.field("realized_pnl", pa.float64()),
        pa.field("currency", pa.string()),
        pa.field("trade_date", pa.string()),
        pa.field("trade_datetime", pa.string()),   # as reported, not parsed
        pa.field("order_time", pa.string()),
        pa.field("open_close", pa.string()),
        pa.field("exchange", pa.string()),
        pa.field("notes", pa.string()),
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

    # One row per (date, root, expiry): the ATM point of the vol surface.
    # Derived rather than captured -- it can always be rebuilt from
    # option_day_bars, which is why it stores the inputs (forward, strike,
    # both leg prices, the rate) beside the output instead of the IV alone.
    atm_term_structure_fields = [
        pa.field("date", pa.string()),             # session the prices are from
        pa.field("underlying", pa.string()),       # OPRA root: SPXW, VIX, ...
        pa.field("expiration_date", pa.string()),
        pa.field("dte", pa.int64()),               # calendar days to expiry
        pa.field("t_years", pa.float64()),         # vol time; convention in daycount
        pa.field("daycount", pa.string()),         # 'bus/252' | 'act/365' (name_for)
        pa.field("forward", pa.float64()),         # put-call parity forward
        pa.field("atm_strike", pa.float64()),      # strike nearest the forward
        pa.field("call_price", pa.float64()),
        pa.field("put_price", pa.float64()),
        pa.field("call_iv", pa.float64()),
        pa.field("put_iv", pa.float64()),
        pa.field("atm_iv", pa.float64()),          # mean of the legs that inverted
        pa.field("rate", pa.float64()),            # r used, from the curve
        pa.field("pairs", pa.int64()),             # strikes quoting both legs
        pa.field("method", pa.string()),           # forward extraction method
        pa.field("src", pa.string()),              # 'day_bars' | 'snapshots'
    ]

    # One row per (date, root, expiry): a raw-SVI fit of that expiry's smile
    # over own-IV OTM strikes. Derived from option_day_bars like
    # atm_term_structure, so it stores the fit inputs (forward, rate, fitted
    # k range) and diagnostics (RMS total-variance residual, the butterfly
    # margin min g(k)) beside the five parameters.
    vol_surface_fields = [
        pa.field("date", pa.string()),             # session the prices are from
        pa.field("underlying", pa.string()),       # SPX | SPXW (European only)
        pa.field("expiration_date", pa.string()),
        pa.field("dte", pa.int64()),               # calendar days to expiry
        pa.field("t_years", pa.float64()),         # vol time; convention in daycount
        pa.field("daycount", pa.string()),         # 'bus/252' | 'act/365' (name_for)
        pa.field("forward", pa.float64()),         # put-call parity forward
        pa.field("svi_a", pa.float64()),           # w(k) = a + b(rho(k-m) + ...)
        pa.field("svi_b", pa.float64()),
        pa.field("svi_rho", pa.float64()),
        pa.field("svi_m", pa.float64()),
        pa.field("svi_sigma", pa.float64()),
        pa.field("k_min", pa.float64()),           # fitted log-moneyness range
        pa.field("k_max", pa.float64()),
        pa.field("n_strikes", pa.int64()),         # OTM points in the fit
        pa.field("rms_error", pa.float64()),       # RMS total-variance residual
        pa.field("min_g", pa.float64()),           # min of Gatheral's g(k)
        pa.field("rate", pa.float64()),            # r used, from the curve
        pa.field("src", pa.string()),              # 'day_bars'
    ]

    # One row per session: SPY cash level. Prefers underlying_day_bars.close
    # (src='bars'); otherwise inverts the shortest-DTE SPY parity forward
    # (src='parity'). resid is proxy-minus-actual on overlap, null elsewhere.
    spy_spot_fields = [
        pa.field("date", pa.string()),
        pa.field("spot", pa.float64()),
        pa.field("forward", pa.float64()),         # shortest-DTE SPY F
        pa.field("dte", pa.int64()),
        pa.field("rate", pa.float64()),            # r used in the proxy
        pa.field("q", pa.float64()),               # resolve_q of (S_proxy, F)
        pa.field("src", pa.string()),              # 'bars' | 'parity'
        pa.field("resid", pa.float64()),           # proxy - actual; null off overlap
    ]

    # One row per decision event (entry / exit / roll / no-trade). Append-only:
    # never quarantine, never as-of (last file would hide earlier events).
    # Nested inputs / signal values / extra version pins are JSON objects,
    # same encoding as additional_underlyings. The strategy engine does not
    # exist yet; the backtester is the first intended writer.
    decision_log_fields = [
        pa.field("decision_id", pa.string()),      # caller-supplied identity
        pa.field("session_date", pa.string()),     # trading session YYYY-MM-DD
        pa.field("asof_ns", pa.int64()),           # decision instant, ns epoch
        pa.field("src", pa.string()),              # 'backtest' | 'paper' | 'live'
        pa.field("job", pa.string()),              # writer name; also in filename
        pa.field("code_version", pa.string()),     # ingest.__version__
        pa.field("underlying", pa.string()),       # OPRA root; null until a book
        pa.field("structure", pa.string()),        # e.g. put_credit_spread
        pa.field("expiration_date", pa.string()),
        pa.field("gate", pa.string()),             # entry | exit | roll | no-trade
        pa.field("rationale", pa.string()),
        pa.field("inputs", pa.string()),           # JSON object
        pa.field("signals", pa.string()),          # JSON object (signal values)
        pa.field("versions", pa.string()),         # JSON object of extra pins
    ]

    contracts_schema = pa.schema(contract_fields)
    return {
        "forwards": pa.schema(forward_fields),
        "atm_term_structure": pa.schema(atm_term_structure_fields),
        "vol_surface": pa.schema(vol_surface_fields),
        "spy_spot": pa.schema(spy_spot_fields),
        "decision_log": pa.schema(decision_log_fields),
        "contracts": contracts_schema,
        "contracts_expired": contracts_schema,  # same schema as contracts
        "option_snapshots": pa.schema(snapshot_fields),
        "option_minute_bars": pa.schema(option_bar_fields),
        "option_day_bars": pa.schema(option_bar_fields),
        "option_trades": pa.schema(trade_fields),
        "underlying_minute_bars": pa.schema(underlying_bar_fields),
        "underlying_day_bars": pa.schema(underlying_day_bar_fields),
        "ibkr_executions": pa.schema(ibkr_execution_fields),
        "treasury_yields": pa.schema(treasury_yield_fields),
        "inflation": pa.schema(inflation_fields),
        "dividends": pa.schema(dividend_fields),
        "splits": pa.schema(split_fields),
    }


# Empty when pyarrow is unavailable; landing.write_clean fails loudly instead.
SCHEMAS: dict[str, Any] = _build_schemas() if pa is not None else {}

# Event history, not a snapshot of current state. ``landing.quarantine_prior``
# and ``catalog.read_asof`` both refuse these: last-file-wins would overwrite
# the decision record the warehouse is required to keep.
APPEND_ONLY_DATASETS: frozenset[str] = frozenset({"decision_log"})

# Closed vocabularies on decision_log.src / .gate. Unknown values fail loud
# in :func:`decision_record` rather than landing a row the backtester cannot
# query later.
DECISION_SOURCES: frozenset[str] = frozenset({"backtest", "paper", "live"})
DECISION_GATES: frozenset[str] = frozenset({"entry", "exit", "roll", "no-trade"})


# ---------------------------------------------------------------------------
# Mapping helpers (fixture -> schema record); shared by jobs and tests.
# ---------------------------------------------------------------------------

def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"decision_log.{field} is required")
    return value


def _json_object(value: Any, field: str) -> str:
    """Encode a JSON object the way ``additional_underlyings`` is stored."""
    if not isinstance(value, dict):
        raise ValueError(
            f"decision_log.{field} must be a JSON object (dict), "
            f"got {type(value).__name__}"
        )
    return json.dumps(value)


def decision_record(
    *,
    decision_id: str,
    session_date: date | str,
    asof_ns: int,
    src: str,
    job: str,
    gate: str,
    rationale: str,
    inputs: dict[str, Any],
    signals: dict[str, Any],
    versions: dict[str, Any] | None = None,
    code_version: str | None = None,
    underlying: str | None = None,
    structure: str | None = None,
    expiration_date: str | None = None,
) -> dict[str, Any]:
    """Build one ``decision_log`` record covering every schema field.

    Nested ``inputs`` / ``signals`` / ``versions`` are JSON-encoded objects.
    ``code_version`` defaults to ``ingest.__version__``. Optional book fields
    (``underlying``, ``structure``, ``expiration_date``) stay null until a
    strategy exists to fill them.
    """
    if isinstance(session_date, date):
        day = session_date.isoformat()
    elif isinstance(session_date, str):
        try:
            date.fromisoformat(session_date)
        except ValueError as exc:
            raise ValueError(
                f"decision_log.session_date must be YYYY-MM-DD, got {session_date!r}"
            ) from exc
        day = session_date
    else:
        raise ValueError("decision_log.session_date must be a date or YYYY-MM-DD string")
    if isinstance(asof_ns, bool) or not isinstance(asof_ns, int):
        raise ValueError("decision_log.asof_ns must be an int (ns epoch)")
    src = _require_text(src, "src")
    if src not in DECISION_SOURCES:
        raise ValueError(
            f"decision_log.src={src!r} is not one of {sorted(DECISION_SOURCES)}"
        )
    gate = _require_text(gate, "gate")
    if gate not in DECISION_GATES:
        raise ValueError(
            f"decision_log.gate={gate!r} is not one of {sorted(DECISION_GATES)}"
        )
    if code_version is None:
        from ingest import __version__ as ingest_version

        code_version = ingest_version
    return {
        "decision_id": _require_text(decision_id, "decision_id"),
        "session_date": day,
        "asof_ns": asof_ns,
        "src": src,
        "job": _require_text(job, "job"),
        "code_version": _require_text(code_version, "code_version"),
        "underlying": underlying,
        "structure": structure,
        "expiration_date": expiration_date,
        "gate": gate,
        "rationale": _require_text(rationale, "rationale"),
        "inputs": _json_object(inputs, "inputs"),
        "signals": _json_object(signals, "signals"),
        "versions": _json_object({} if versions is None else versions, "versions"),
    }


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
    "APPEND_ONLY_DATASETS",
    "DECISION_SOURCES",
    "DECISION_GATES",
    "decision_record",
    "flatten_snapshot",
    "contract_record",
    "pa",
]
