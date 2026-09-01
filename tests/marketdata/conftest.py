"""Parquet helpers for marketdata catalog tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from ingest.schemas import SCHEMAS


def snapshot_row(
    ticker: str,
    *,
    strike: float = 420.0,
    expiry: str = "2026-08-31",
    underlying: str = "SPY",
    cp: str = "call",
    vendor_iv: float | None = 0.17,
    vendor_delta: float | None = 0.5,
) -> dict[str, Any]:
    """Minimal option_snapshots record covering every schema field."""
    schema = SCHEMAS["option_snapshots"]
    rec = {f.name: None for f in schema}
    rec.update(
        {
            "ticker": ticker,
            "details_contract_type": cp,
            "details_exercise_style": "american" if underlying == "SPY" else "european",
            "details_expiration_date": expiry,
            "details_strike_price": strike,
            "details_shares_per_contract": 100,
            "day_close": 10.0,
            "last_trade_price": 10.0,
            "underlying_ticker": underlying,
            "underlying_price": 500.0,
            "underlying_last_updated_ns": 1_700_000_000_000_000_000,
            "greeks_delta": vendor_delta,
            "greeks_gamma": 0.01,
            "greeks_theta": -0.02,
            "greeks_vega": 0.15,
            "implied_volatility": vendor_iv,
            "open_interest": 10,
        }
    )
    return rec


def contract_row(ticker: str, *, strike: float = 420.0, underlying: str = "SPY") -> dict[str, Any]:
    schema = SCHEMAS["contracts"]
    rec = {f.name: None for f in schema}
    rec.update(
        {
            "ticker": ticker,
            "underlying_ticker": underlying,
            "contract_type": "call",
            "exercise_style": "american" if underlying == "SPY" else "european",
            "expiration_date": "2026-08-31",
            "strike_price": strike,
            "shares_per_contract": 100,
        }
    )
    return rec


def forward_row(
    *,
    underlying: str = "SPY",
    expiry: str = "2026-09-18",
    forward: float = 500.0,
    atm_strike: float | None = None,
    asof_ns: int = 1_700_000_000_000_000_000,
    method: str = "parity",
) -> dict[str, Any]:
    """Minimal forwards record covering every schema field."""
    schema = SCHEMAS["forwards"]
    rec = {f.name: None for f in schema}
    rec.update(
        {
            "underlying_ticker": underlying,
            "expiration_date": expiry,
            "atm_strike": atm_strike if atm_strike is not None else forward,
            "forward": forward,
            "call_price": 10.0,
            "put_price": 10.0,
            "pairs": 10,
            "asof_ns": asof_ns,
            "method": method,
        }
    )
    return rec


def write_records(
    path: Path,
    dataset: str,
    records: list[dict[str, Any]],
    extra: dict[str, list[Any]] | None = None,
    drop: str | None = None,
) -> None:
    schema = SCHEMAS[dataset]
    projected = [{f.name: r.get(f.name) for f in schema} for r in records]
    table = pa.Table.from_pylist(projected, schema=schema)
    if drop:
        names = [n for n in table.column_names if n != drop]
        table = table.select(names)
    if extra:
        for name, values in extra.items():
            table = table.append_column(name, pa.array(values))
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def partition_path(data_root: Path, dataset: str, dt: date, name: str) -> Path:
    return data_root / "clean" / dataset / f"dt={dt.isoformat()}" / name
