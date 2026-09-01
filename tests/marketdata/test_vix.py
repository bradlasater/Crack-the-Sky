"""VIX roots, AM settlement, and the parity forward being the VX future.

VIX option trades were already inside the flat files we download (14,202 O:VIX
and 4,419 O:VIXW on 2026-08-28); keep_ticker() simply discarded them.
"""

from __future__ import annotations

import datetime as dt

import pytest

from ingest.jobs import OPTION_ROOTS, keep_ticker, ticker_root
from marketdata.opra import (
    ALLOWED_ROOTS,
    SETTLEMENT_ET,
    OPRAParseError,
    parse_opra,
    settlement_time_et,
)
from pricing.from_market import expiry_instant

# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("root", ["VIX", "VIXW"])
def test_vix_roots_are_allowed_everywhere(root: str) -> None:
    assert root in ALLOWED_ROOTS
    assert root in OPTION_ROOTS
    assert root in SETTLEMENT_ET


@pytest.mark.parametrize(
    ("ticker", "keep", "root"),
    [
        ("O:VIX260916C00020000", True, "VIX"),
        ("O:VIXW260902P00016000", True, "VIXW"),
        # ProShares VIX Short-Term Futures ETF -- a different underlying, and
        # the exact trap a startswith("O:VIX") prefix would fall into.
        ("O:VIXY260918C00020000", False, "VIXY"),
        ("O:VXX260918C00050000", False, "VXX"),
    ],
)
def test_vix_prefix_trap(ticker: str, keep: bool, root: str) -> None:
    assert keep_ticker(ticker) is keep
    assert ticker_root(ticker) == root


def test_vixw_wins_against_vix_in_the_parser() -> None:
    """Longest-first: VIXW must not parse as root VIX with a stray W."""
    assert parse_opra("O:VIXW260902P00016000").root == "VIXW"
    assert parse_opra("O:VIX260916C00020000").root == "VIX"


def test_vix_etf_is_rejected_by_the_parser() -> None:
    with pytest.raises(OPRAParseError):
        parse_opra("O:VIXY260918C00020000")


# ---------------------------------------------------------------------------
# Settlement: VIX is AM settled, both series
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("root", ["VIX", "VIXW"])
def test_vix_is_am_settled(root: str) -> None:
    """Both VIX series settle to the SOQ at the Wednesday open.

    Unlike SPX/SPXW, where the weeklies are PM settled. Assuming 16:00 here
    would reintroduce the ~20h T error across the whole VIX surface.
    """
    assert settlement_time_et(root) == (9, 30)


def test_vix_expiry_instant_is_the_morning_soq() -> None:
    inst = expiry_instant(parse_opra("O:VIX260916C00020000"))
    assert inst == dt.datetime(2026, 9, 16, 13, 30, tzinfo=dt.UTC)   # 09:30 ET


def test_vix_weekly_settles_earlier_in_the_day_than_an_spxw() -> None:
    vix = expiry_instant(parse_opra("O:VIXW260916P00016000"))
    spxw = expiry_instant(parse_opra("O:SPXW260916P07600000"))
    assert vix < spxw
    assert (spxw - vix) == dt.timedelta(hours=6, minutes=30)


# ---------------------------------------------------------------------------
# Contract semantics
# ---------------------------------------------------------------------------

def test_vix_options_are_european_on_the_vix_future() -> None:
    """They are options on the VX future of that expiry, not on the index.

    The per-expiry parity forward therefore *is* the VX future, which is the
    object a term-structure model wants. Nothing should try to discount it
    toward a spot VIX -- the vendor does not even supply the index here.
    """
    c = parse_opra("O:VIX260916C00020000")
    assert c.exercise_style == "european"
    assert c.underlying == "VIX"
    assert parse_opra("O:VIXW260902P00016000").underlying == "VIX"


def test_forward_from_parity_recovers_a_vix_curve() -> None:
    """Synthetic chain priced at intrinsic against a rising forward curve."""
    from ingest.jobs import forward_from_parity

    rows = []
    for exp, fwd in (("2026-09-02", 15.26), ("2026-09-09", 16.28), ("2026-09-16", 16.58)):
        for strike in (14.0, 15.0, 16.0, 17.0, 18.0):
            for kind in ("call", "put"):
                intrinsic = max(fwd - strike, 0.0) if kind == "call" else max(strike - fwd, 0.0)
                rows.append({
                    "ticker": f"O:VIX{exp[2:4]}{exp[5:7]}{exp[8:10]}"
                              f"{'C' if kind == 'call' else 'P'}{int(strike * 1000):08d}",
                    "details_expiration_date": exp,
                    "details_strike_price": strike,
                    "details_contract_type": kind,
                    "day_close": intrinsic,
                    "underlying_ticker": "VIX",
                    "underlying_price": None,     # never populated for VIX
                    "day_last_updated_ns": 1788212108195000000,
                })
    fwds = {f["expiration_date"]: f["forward"] for f in forward_from_parity(rows)}
    assert fwds["2026-09-02"] == pytest.approx(15.26)
    assert fwds["2026-09-09"] == pytest.approx(16.28)
    assert fwds["2026-09-16"] == pytest.approx(16.58)
    # contango: the curve must come out ordered, not scrambled by expiry sort
    assert fwds["2026-09-02"] < fwds["2026-09-09"] < fwds["2026-09-16"]
