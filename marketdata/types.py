"""Contract, Quote, and Forward dataclasses. No pandas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

CallPut = Literal["call", "put"]
ExerciseStyle = Literal["european", "american"]

# Snapshot columns that are vendor diagnostics, never pricing inputs.
VENDOR_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "greeks_delta",
    "greeks_gamma",
    "greeks_theta",
    "greeks_vega",
    "implied_volatility",
)


@dataclass(frozen=True, slots=True)
class Contract:
    """One OPRA listed option. Style and multiplier are explicit, not inferred later."""

    root: str
    underlying: str
    expiry: date
    call_put: CallPut
    strike: float
    exercise_style: ExerciseStyle
    multiplier: int
    ticker: str = ""


@dataclass(frozen=True, slots=True)
class Quote:
    """A snapshot-derived quote. Vendor IV/greeks are diagnostics only."""

    contract: Contract
    last: float | None
    day_close: float | None
    underlying_price: float | None
    asof_ns: int | None
    open_interest: int | None
    vendor_implied_volatility: float | None = None
    vendor_delta: float | None = None
    vendor_gamma: float | None = None
    vendor_theta: float | None = None
    vendor_vega: float | None = None

    @property
    def market_price(self) -> float | None:
        """Last trade if present, else the session close. Not vendor IV."""
        if self.last is not None:
            return self.last
        return self.day_close


@dataclass(frozen=True, slots=True)
class Forward:
    """Per-expiry forward recovered from put-call parity (or spot/proxy)."""

    underlying: str
    expiry: date
    atm_strike: float
    forward: float
    call_price: float
    put_price: float
    pairs: int
    asof_ns: int | None
    method: str


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def quotes_from_snapshot_rows(rows: Any) -> list[Quote]:
    """Map option_snapshots records to :class:`Quote`. Parses every ticker.

    Accepts a list of dicts or a ``pyarrow.Table``. The previous name said
    "table" while the parameter was rows, so passing an actual Table failed
    deep inside with ``'ChunkedArray' object has no attribute 'get'``.

    Vendor greeks/IV are copied onto the Quote and must stay unused by
    :mod:`pricing`.
    """
    if hasattr(rows, "to_pylist"):  # pyarrow.Table / RecordBatch
        rows = rows.to_pylist()
    # Imported here to keep types importable without the parser cycle... there
    # is no cycle if opra imports Contract from here; this helper needs parse.
    from marketdata.opra import parse_opra

    out: list[Quote] = []
    for rec in rows:
        ticker = rec.get("ticker")
        contract = parse_opra(str(ticker))
        asof = (
            rec.get("underlying_last_updated_ns")
            or rec.get("last_trade_sip_timestamp_ns")
            or rec.get("day_last_updated_ns")
        )
        out.append(
            Quote(
                contract=contract,
                last=_opt_float(rec.get("last_trade_price")),
                day_close=_opt_float(rec.get("day_close")),
                underlying_price=_opt_float(rec.get("underlying_price")),
                asof_ns=_opt_int(asof),
                open_interest=_opt_int(rec.get("open_interest")),
                vendor_implied_volatility=_opt_float(rec.get("implied_volatility")),
                vendor_delta=_opt_float(rec.get("greeks_delta")),
                vendor_gamma=_opt_float(rec.get("greeks_gamma")),
                vendor_theta=_opt_float(rec.get("greeks_theta")),
                vendor_vega=_opt_float(rec.get("greeks_vega")),
            )
        )
    return out


def forward_from_record(rec: dict[str, Any]) -> Forward:
    """Map a ``forwards`` parquet row to :class:`Forward`."""
    exp_raw = rec.get("expiration_date")
    if not exp_raw:
        raise ValueError("forwards row missing expiration_date")
    return Forward(
        underlying=str(rec.get("underlying_ticker") or ""),
        expiry=date.fromisoformat(str(exp_raw)[:10]),
        atm_strike=float(rec["atm_strike"]),
        forward=float(rec["forward"]),
        call_price=float(rec["call_price"]),
        put_price=float(rec["put_price"]),
        pairs=int(rec["pairs"]),
        asof_ns=_opt_int(rec.get("asof_ns")),
        method=str(rec.get("method") or ""),
    )


# Back-compat alias for the pre-rename name.
quotes_from_snapshot_table = quotes_from_snapshot_rows
