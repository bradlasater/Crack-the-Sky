"""OPRA parser: allowlist, adversaries, round-trip."""

from __future__ import annotations

from datetime import date

import pytest

from marketdata.opra import (
    ALLOWED_ROOTS,
    MULTIPLIER,
    OPRAParseError,
    format_opra,
    parse_opra,
    ticker_root,
)


def test_spy_call() -> None:
    c = parse_opra("O:SPY260831C00420000")
    assert c.root == "SPY"
    assert c.underlying == "SPY"
    assert c.expiry == date(2026, 8, 31)
    assert c.call_put == "call"
    assert c.strike == pytest.approx(420.0)
    assert c.exercise_style == "american"
    assert c.multiplier == MULTIPLIER == 100
    assert format_opra(c) == "O:SPY260831C00420000"


def test_spx_european() -> None:
    c = parse_opra("O:SPX260918C07000000")
    assert c.root == "SPX"
    assert c.underlying == "SPX"
    assert c.exercise_style == "european"
    assert c.strike == pytest.approx(7000.0)


def test_spxw_is_not_spx() -> None:
    c = parse_opra("O:SPXW260918P07000000")
    assert c.root == "SPXW"
    assert c.underlying == "SPX"
    assert c.call_put == "put"
    assert c.exercise_style == "european"
    assert ticker_root("O:SPXW260918P07000000") == "SPXW"
    assert ticker_root("O:SPX260918C07000000") == "SPX"


@pytest.mark.parametrize(
    "ticker",
    [
        "O:SPYL260831C00420000",
        "O:SPXS260831P00420000",
        "O:SPYG260831C00420000",
        "O:SPXL260831C00420000",
    ],
)
def test_foreign_roots_rejected(ticker: str) -> None:
    root = ticker_root(ticker)
    assert root is not None and root not in ALLOWED_ROOTS
    with pytest.raises(OPRAParseError, match="foreign OPRA root"):
        parse_opra(ticker)


@pytest.mark.parametrize(
    "ticker",
    [
        "",
        "O:SPY",
        "SPY260831C00420000",
        "O:SPY260831X00420000",
        "O:SPY260831C",
        "O:SPY269931C00420000",
        "O:SPY260832C00420000",
    ],
)
def test_malformed_rejected(ticker: str) -> None:
    with pytest.raises(OPRAParseError):
        parse_opra(ticker)


def test_quotes_from_snapshot_rows_accepts_a_pyarrow_table() -> None:
    """The old name said 'table' but the parameter was rows.

    Passing an actual Table failed deep inside with
    ``'ChunkedArray' object has no attribute 'get'``.
    """
    import pyarrow as pa

    from marketdata.types import quotes_from_snapshot_rows, quotes_from_snapshot_table

    row = {
        "ticker": "O:SPY260918C00770000",
        "last_trade_price": 6.87,
        "day_close": 6.87,
        "underlying_price": 767.38,
        "underlying_last_updated_ns": 1788215198897256807,
        "open_interest": 17524,
    }
    table = pa.Table.from_pylist([row])
    from_rows = quotes_from_snapshot_rows([row])
    from_table = quotes_from_snapshot_rows(table)
    assert len(from_rows) == len(from_table) == 1
    assert from_rows[0].contract.ticker == from_table[0].contract.ticker
    # Old name stays importable.
    assert quotes_from_snapshot_table is quotes_from_snapshot_rows
