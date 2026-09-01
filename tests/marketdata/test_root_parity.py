"""The root allowlist must not drift between ingest and marketdata.

Two definitions of "which options are ours" is how SPXW -- ~98% of SPX option
trade volume -- went missing from four years of manual pulls without anyone
noticing. These bind the two together.
"""

from __future__ import annotations

import pytest

from ingest.jobs import OPTION_ROOTS, keep_ticker
from ingest.jobs import ticker_root as ingest_root
from marketdata.opra import ALLOWED_ROOTS, SETTLEMENT_ET
from marketdata.opra import ticker_root as marketdata_root


def test_allowlists_are_identical() -> None:
    assert set(ALLOWED_ROOTS) == set(OPTION_ROOTS)


def test_every_allowed_root_has_a_settlement_time() -> None:
    assert set(SETTLEMENT_ET) == set(ALLOWED_ROOTS)


@pytest.mark.parametrize(
    "ticker",
    [
        "O:SPY260918C00770000",
        "O:SPX260918C08000000",
        "O:SPXW260918P07600000",
        "O:SPXL260918C00250000",   # Direxion 3x, not SPX
        "O:SPXS260918P00010000",
        "O:SPYG260918C00100000",
        "O:QQQ260918C00500000",
        "not-a-ticker",
        "",
    ],
)
def test_ticker_root_agrees_across_packages(ticker: str) -> None:
    assert marketdata_root(ticker) == ingest_root(ticker)


@pytest.mark.parametrize(
    "ticker",
    ["O:SPXL260918C00250000", "O:SPXS260918P00010000", "O:SPYG260918C00100000"],
)
def test_foreign_roots_rejected_by_both(ticker: str) -> None:
    assert not keep_ticker(ticker)
    assert marketdata_root(ticker) not in ALLOWED_ROOTS
