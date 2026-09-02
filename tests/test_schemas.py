"""Schema tests: every dataset maps its frozen fixture to a complete record.

Each mapping below turns one real (truncated) API result into a schema record;
tests assert no schema field is missing from the record, that pyarrow accepts
it, and spot-check representative values. Offline: fixtures only, no network.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pa = pytest.importorskip("pyarrow", reason="pyarrow required for schema tests")

from ingest import schemas  # noqa: E402
from tests.conftest import load_fixture  # noqa: E402

DATASETS = [
    "contracts",
    "contracts_expired",
    "option_snapshots",
    "option_minute_bars",
    "option_day_bars",
    "option_trades",
    "underlying_minute_bars",
    "underlying_day_bars",
    "ibkr_executions",
    "treasury_yields",
    "inflation",
    "forwards",
    "atm_term_structure",
    "dividends",
    "splits",
]


def check_records(dataset: str, records: list[dict[str, Any]]) -> pa.Table:
    """Assert records cover every schema field and build a typed table."""
    schema = schemas.SCHEMAS[dataset]
    names = {f.name for f in schema}
    for rec in records:
        missing = names - set(rec)
        assert not missing, f"{dataset}: record missing schema fields {missing}"
    table = pa.Table.from_pylist(
        [{f.name: r.get(f.name) for f in schema} for r in records], schema=schema
    )
    assert table.num_rows == len(records)
    assert table.schema.equals(schema)
    return table


def test_all_datasets_present() -> None:
    assert set(schemas.SCHEMAS) == set(DATASETS)


def test_contracts_schema_matches_fixture() -> None:
    results = load_fixture("contracts.json")["results"]
    records = [schemas.contract_record(r) for r in results]
    for dataset in ("contracts", "contracts_expired"):  # same schema
        table = check_records(dataset, records)
        assert table["ticker"].to_pylist()[0] == "O:SPY260831C00420000"
        assert table["underlying_ticker"].to_pylist()[0] == "SPY"
        assert table["strike_price"].to_pylist()[0] == pytest.approx(420.0)
        assert table["shares_per_contract"].to_pylist()[0] == 100


def test_option_snapshots_schema_matches_fixture() -> None:
    results = load_fixture("snapshot_options_spy.json")["results"]
    records = [schemas.flatten_snapshot(r) for r in results]
    table = check_records("option_snapshots", records)
    first = {k: v[0] for k, v in table.to_pydict().items()}
    assert first["ticker"] == "O:SPY260831C00420000"
    assert first["details_contract_type"] == "call"
    assert first["details_expiration_date"] == "2026-08-31"
    assert first["day_close"] == pytest.approx(345.24)
    assert first["last_trade_size"] == 1
    assert first["last_trade_sip_timestamp_ns"] == 1787319513276922706
    assert json.loads(first["last_trade_conditions"]) == [232]
    assert first["open_interest"] == 4
    assert first["underlying_ticker"] == "SPY"
    assert first["underlying_price"] == pytest.approx(765.739)
    # greeks are delivered empty on this tier: columns exist, values null
    for col in ("greeks_delta", "greeks_gamma", "greeks_theta", "greeks_vega",
                "implied_volatility"):
        assert first[col] is None


def ws_am_to_record(ev: dict[str, Any]) -> dict[str, Any]:
    """Map a WS aggregate-minute (AM) event to an option_minute_bars record."""
    return {
        "ticker": ev["sym"],
        "window_start_ns": ev["s"],
        "window_end_ns": ev["e"],
        "open": ev["o"],
        "high": ev["h"],
        "low": ev["l"],
        "close": ev["c"],
        "volume": ev["v"],
        "vwap": ev.get("vw"),
        "transactions": ev.get("z"),
        "official_open": ev.get("op"),
        "accumulated_volume": ev.get("av"),
        "src": "ws",
    }


def test_option_minute_bars_ws_event_maps() -> None:
    event = {
        "ev": "AM", "sym": "O:SPY260918C00765000", "v": 158, "av": 937321,
        "op": 8.93, "vw": 8.9618, "o": 8.95, "c": 8.9, "h": 8.96, "l": 8.89,
        "a": 8.9618, "z": 42, "s": 1787904060000000000, "e": 1787904120000000000,
    }
    table = check_records("option_minute_bars", [ws_am_to_record(event)])
    row = {k: v[0] for k, v in table.to_pydict().items()}
    assert row["ticker"] == "O:SPY260918C00765000"
    assert row["window_start_ns"] == 1787904060000000000
    assert row["window_end_ns"] == 1787904120000000000
    assert row["transactions"] == 42
    assert row["src"] == "ws"


def rest_agg_to_option_day_bar(ticker: str, r: dict[str, Any]) -> dict[str, Any]:
    """Map one REST agg result (t = ms epoch) to an option_day_bars record."""
    return {
        "ticker": ticker,
        "window_start_ns": r["t"] * 1_000_000,  # ms -> ns, no timezone games
        "window_end_ns": None,
        "open": r["o"], "high": r["h"], "low": r["l"], "close": r["c"],
        "volume": r["v"], "vwap": r.get("vw"), "transactions": r.get("n"),
        "official_open": None, "accumulated_volume": None, "src": "rest",
    }


def test_option_day_bars_rest_agg_maps() -> None:
    results = load_fixture("aggs_spy_minute.json")["results"]
    records = [rest_agg_to_option_day_bar("O:SPY260918C00765000", r) for r in results]
    table = check_records("option_day_bars", records)
    assert table["window_start_ns"].to_pylist()[0] == 1787904000000 * 1_000_000
    assert table["src"].to_pylist() == ["rest"] * len(results)


def test_underlying_minute_bars_schema_matches_fixture() -> None:
    payload = load_fixture("aggs_spy_minute.json")
    records = [{
        "ticker": payload["ticker"],
        "start_ms": r["t"],  # stored exactly as delivered (ms epoch)
        "open": r["o"], "high": r["h"], "low": r["l"], "close": r["c"],
        "volume": r["v"], "vwap": r.get("vw"), "transactions": r.get("n"),
    } for r in payload["results"]]
    table = check_records("underlying_minute_bars", records)
    assert table["ticker"].to_pylist()[0] == "SPY"
    assert table["start_ms"].to_pylist()[0] == 1787904000000
    assert table["close"].to_pylist()[0] == pytest.approx(770.7)
    assert table["transactions"].to_pylist()[0] == 830


def test_option_trades_schema_matches_fixture() -> None:
    payload = load_fixture("trades.json")
    assert payload["status"] == "DELAYED"  # tier reality: 15-min delayed
    records = [{
        "ticker": "O:SPY260918C00765000",  # from the request path
        "price": r["price"],
        "size": r["size"],
        "exchange": r.get("exchange"),
        "conditions": json.dumps(r.get("conditions")) if r.get("conditions") is not None else None,
        "correction": r.get("correction"),
        "trade_id": r.get("id") or None,
        "sequence_number": r.get("sequence_number"),
        "sip_timestamp_ns": r.get("sip_timestamp"),
        "participant_timestamp_ns": r.get("participant_timestamp"),  # absent delayed
        "src": "rest",
    } for r in payload["results"]]
    table = check_records("option_trades", records)
    assert table["price"].to_pylist()[0] == pytest.approx(8.9)
    assert table["size"].to_pylist()[0] == 3
    assert table["sip_timestamp_ns"].to_pylist()[0] == 1788202655938744983
    assert json.loads(table["conditions"].to_pylist()[0]) == [209]


def test_dividends_schema_matches_fixture() -> None:
    results = load_fixture("dividends.json")["results"]
    records = [{
        "ticker": r["ticker"],
        "dividend_id": r.get("id"),
        "cash_amount": r.get("cash_amount"),
        "currency": r.get("currency"),
        "dividend_type": r.get("dividend_type"),
        "frequency": r.get("frequency"),
        "declaration_date": r.get("declaration_date"),
        "ex_dividend_date": r.get("ex_dividend_date"),
        "record_date": r.get("record_date"),
        "pay_date": r.get("pay_date"),
    } for r in results]
    table = check_records("dividends", records)
    assert table["ticker"].to_pylist()[0] == "SPY"
    assert table["cash_amount"].to_pylist()[0] == pytest.approx(1.903516)
    assert table["ex_dividend_date"].to_pylist()[0] == "2026-06-18"


def test_splits_schema_matches_fixture() -> None:
    results = load_fixture("splits.json")["results"]
    records = [{
        "ticker": r["ticker"],
        "split_id": r.get("id"),
        "execution_date": r.get("execution_date"),
        "split_from": r.get("split_from"),
        "split_to": r.get("split_to"),
    } for r in results]
    table = check_records("splits", records)
    assert table["ticker"].to_pylist()[0] == "AAPL"
    assert table["split_to"].to_pylist()[0] == pytest.approx(4.0)
    assert table["execution_date"].to_pylist()[0] == "2020-08-31"


def test_timestamp_fields_use_ns_ms_suffixes() -> None:
    for dataset, schema in schemas.SCHEMAS.items():
        for field in schema:
            if field.type == pa.int64() and any(
                tok in field.name for tok in ("timestamp", "updated", "start", "end")
            ) and field.name not in ("sequence_number",):
                assert field.name.endswith(("_ns", "_ms")), (
                    f"{dataset}.{field.name} epoch field must end _ns/_ms"
                )


def test_src_column_on_bars_and_trades() -> None:
    for dataset in ("option_minute_bars", "option_day_bars", "option_trades"):
        field = schemas.SCHEMAS[dataset].field("src")
        assert field.type == pa.string()
