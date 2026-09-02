"""Tests for the ATM term structure built off option_day_bars.

The reduction is only worth anything if it recovers a vol it was not given,
so the core test prices a synthetic chain at a known vol and asserts the
inversion returns it. The rest pin the failure modes that would otherwise
produce a plausible-looking but wrong number.
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from ingest.jobs import parse_option_ticker
from pricing import term_structure as ts
from pricing.bsm import price

DAY = date(2026, 8, 28)
EXPIRY = date(2026, 9, 25)
DTE = (EXPIRY - DAY).days
T = DTE / 365.0
R = 0.04
F = 7700.0
VOL = 0.18


def _flat_rate(_as_of, _T):  # noqa: ANN001
    return R


def _sym(root: str, expiry: date, kind: str, strike: float) -> str:
    return (f"O:{root}{expiry:%y%m%d}{'C' if kind == 'call' else 'P'}"
            f"{int(round(strike * 1000)):08d}")


def _chain_bars(root: str = "SPXW", vol: float = VOL, strikes=None) -> list[dict]:
    """Day bars for a chain priced at ``vol`` in the forward measure."""
    strikes = strikes if strikes is not None else [7600.0, 7650.0, 7700.0, 7750.0]
    bars = []
    for k in strikes:
        for kind in ("call", "put"):
            # S=F with q=r is Black-76: exactly what term_structure inverts.
            px = float(price(F, k, T, R, vol, kind, q=R))
            bars.append({"ticker": _sym(root, EXPIRY, kind, k),
                         "close": px, "window_end_ns": 1})
    return bars


# ---------------------------------------------------------------------------
# OPRA parsing -- the only route to strike/expiry for four years of history
# ---------------------------------------------------------------------------

def test_parses_a_full_opra_symbol() -> None:
    assert parse_option_ticker("O:SPXW260918C05000000") == {
        "root": "SPXW",
        "expiration_date": "2026-09-18",
        "contract_type": "call",
        "strike": 5000.0,
    }


def test_parses_fractional_strike() -> None:
    """Strikes are thousandths; SPY has half-dollar strikes."""
    assert parse_option_ticker("O:SPY260918P00769500")["strike"] == 769.5


@pytest.mark.parametrize("bad", [
    "", None, "SPY", "O:SPY260918X00769500", "O:SPY2609C00769500",
    "O:SPXW261318C05000000",  # month 13
])
def test_rejects_non_options(bad) -> None:
    assert parse_option_ticker(bad) is None


def test_round_trips_against_the_symbol_builder() -> None:
    sym = _sym("VIXW", date(2027, 1, 20), "put", 21.5)
    got = parse_option_ticker(sym)
    assert (got["root"], got["expiration_date"], got["contract_type"],
            got["strike"]) == ("VIXW", "2027-01-20", "put", 21.5)


# ---------------------------------------------------------------------------
# The inversion recovers the vol it was priced at
# ---------------------------------------------------------------------------

def test_recovers_the_vol_the_chain_was_priced_at() -> None:
    rows = ts.build_rows(_chain_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate)
    assert len(rows) == 1
    row = rows[0]
    assert row["atm_iv"] == pytest.approx(VOL, abs=1e-6)
    assert row["call_iv"] == pytest.approx(VOL, abs=1e-6)
    assert row["put_iv"] == pytest.approx(VOL, abs=1e-6)


def test_recovers_the_forward_from_parity() -> None:
    rows = ts.build_rows(_chain_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate)
    assert rows[0]["forward"] == pytest.approx(F, rel=1e-9)
    assert rows[0]["atm_strike"] == 7700.0


def test_dte_and_t_years_are_act_365() -> None:
    row = ts.build_rows(_chain_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate)[0]
    assert row["dte"] == DTE
    assert row["t_years"] == pytest.approx(DTE / 365.0)


@pytest.mark.parametrize("vol", [0.08, 0.18, 0.45, 0.90])
def test_recovers_across_the_vol_range(vol: float) -> None:
    rows = ts.build_rows(_chain_bars(vol=vol), DAY, roots=("SPXW",), rate_fn=_flat_rate)
    assert rows[0]["atm_iv"] == pytest.approx(vol, abs=1e-5)


# ---------------------------------------------------------------------------
# Roots stay separate; SPXW and SPX are different instruments
# ---------------------------------------------------------------------------

def test_roots_are_not_merged() -> None:
    bars = _chain_bars("SPXW") + _chain_bars("SPX", vol=0.25)
    rows = ts.build_rows(bars, DAY, roots=("SPX", "SPXW"), rate_fn=_flat_rate)
    got = {r["underlying"]: r["atm_iv"] for r in rows}
    assert got["SPXW"] == pytest.approx(VOL, abs=1e-5)
    assert got["SPX"] == pytest.approx(0.25, abs=1e-5)


def test_unrequested_roots_are_skipped() -> None:
    bars = _chain_bars("SPXW") + _chain_bars("SPY")
    rows = ts.build_rows(bars, DAY, roots=("SPY",), rate_fn=_flat_rate)
    assert {r["underlying"] for r in rows} == {"SPY"}


def test_non_option_tickers_are_ignored() -> None:
    bars = _chain_bars() + [{"ticker": "SPXL", "close": 1.0, "window_end_ns": 1}]
    rows = ts.build_rows(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Failure modes that must not fabricate a number
# ---------------------------------------------------------------------------

def test_same_day_expiry_is_skipped_not_priced_at_t_zero() -> None:
    """T=0 has no vol that reproduces any price; the row must not appear."""
    bars = []
    for k in (7690.0, 7700.0):
        for kind in ("call", "put"):
            intrinsic = max(F - k, 0.0) if kind == "call" else max(k - F, 0.0)
            bars.append({"ticker": _sym("SPXW", DAY, kind, k),
                         "close": intrinsic or 0.05, "window_end_ns": 1})
    assert ts.build_rows(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate) == []


def test_stale_atm_print_is_null_rather_than_zero_vol() -> None:
    """An ATM close at the intrinsic floor inverts to 0.0; that is not a vol.

    The floor for an at-the-money option is ~0, so a stale or missing print
    sits inside the arbitrage bounds and the solver returns zero rather than
    raising. Recorded as 0.0 it would claim the market implied no volatility
    and would drag any downstream average toward zero.
    """
    bars = _chain_bars()
    for b in bars:
        terms = parse_option_ticker(b["ticker"])
        if terms["contract_type"] == "put" and terms["strike"] == 7700.0:
            b["close"] = 1e-9
    rows = ts.build_rows(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate)
    assert rows[0]["put_iv"] is None
    assert rows[0]["call_iv"] is not None
    # The mean must come from the leg that inverted, not be dragged to zero.
    assert rows[0]["atm_iv"] == pytest.approx(rows[0]["call_iv"])


def test_crossed_print_above_the_bound_is_null() -> None:
    """A price above the no-arbitrage ceiling raises out of the solver."""
    bars = _chain_bars()
    for b in bars:
        terms = parse_option_ticker(b["ticker"])
        if terms["contract_type"] == "call" and terms["strike"] == 7700.0:
            b["close"] = F * 10  # far above any arbitrage-free call value
    rows = ts.build_rows(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate)
    assert rows[0]["call_iv"] is None
    assert rows[0]["put_iv"] is not None


def test_expiry_with_no_paired_strike_yields_no_row() -> None:
    """Parity needs both legs; a calls-only expiry cannot produce a forward."""
    bars = [b for b in _chain_bars()
            if parse_option_ticker(b["ticker"])["contract_type"] == "call"]
    assert ts.build_rows(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate) == []


def test_zero_and_negative_closes_are_dropped() -> None:
    bars = _chain_bars() + [
        {"ticker": _sym("SPXW", EXPIRY, "call", 8000.0), "close": 0.0,
         "window_end_ns": 1},
        {"ticker": _sym("SPXW", EXPIRY, "put", 8000.0), "close": -1.0,
         "window_end_ns": 1},
    ]
    rows = ts.build_rows(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate)
    assert rows[0]["atm_strike"] == 7700.0, "the dropped strike must not win ATM"


def test_empty_input_is_empty_output_not_an_error() -> None:
    assert ts.build_rows([], DAY, roots=("SPXW",), rate_fn=_flat_rate) == []


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------

def test_rows_match_the_landed_schema() -> None:
    from ingest import schemas

    rows = ts.build_rows(_chain_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate)
    fields = {f.name for f in schemas.SCHEMAS[ts.DATASET]}
    assert set(rows[0]) == fields, "extra keys are dropped silently on write"


def test_term_structure_slopes_with_expiry() -> None:
    """Two expiries at different vols must come back in the right order."""
    far = date(2026, 11, 20)
    far_T = (far - DAY).days / 365.0
    bars = _chain_bars()
    for k in (7650.0, 7700.0, 7750.0):
        for kind in ("call", "put"):
            px = float(price(F, k, far_T, R, 0.24, kind, q=R))
            bars.append({"ticker": _sym("SPXW", far, kind, k),
                         "close": px, "window_end_ns": 1})
    rows = ts.build_rows(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate)
    by_expiry = {r["expiration_date"]: r["atm_iv"] for r in rows}
    assert by_expiry["2026-09-25"] == pytest.approx(VOL, abs=1e-5)
    assert by_expiry["2026-11-20"] == pytest.approx(0.24, abs=1e-5)
    assert not math.isclose(by_expiry["2026-09-25"], by_expiry["2026-11-20"])


# ---------------------------------------------------------------------------
# ATM must be a strike with both legs
# ---------------------------------------------------------------------------

def test_atm_skips_a_nearer_one_sided_strike() -> None:
    """Day bars hold only contracts that traded, so the nearest strike is
    often call-only or put-only. Picking it would average a single leg and
    call the result an ATM point."""
    bars = _chain_bars(strikes=[7650.0, 7700.0])
    # A call-only strike nearer the forward than any paired strike.
    bars.append({"ticker": _sym("SPXW", EXPIRY, "call", 7695.0),
                 "close": float(price(F, 7695.0, T, R, VOL, "call", q=R)),
                 "window_end_ns": 1})
    row = ts.build_rows(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate)[0]
    assert row["atm_strike"] == 7700.0, "one-sided 7695 must not win ATM"
    assert row["call_iv"] is not None and row["put_iv"] is not None


def test_both_legs_present_on_every_row() -> None:
    """The dataset documents a two-leg ATM point; prices must never be null."""
    bars = _chain_bars(strikes=[7600.0, 7650.0, 7700.0])
    bars.append({"ticker": _sym("SPXW", EXPIRY, "put", 7702.0),
                 "close": float(price(F, 7702.0, T, R, VOL, "put", q=R)),
                 "window_end_ns": 1})
    for row in ts.build_rows(bars, DAY, roots=("SPXW",), rate_fn=_flat_rate):
        assert row["call_price"] is not None
        assert row["put_price"] is not None


# ---------------------------------------------------------------------------
# Re-running a date must not double-count
# ---------------------------------------------------------------------------

def test_second_write_replaces_rather_than_appends(tmp_path) -> None:
    """write_clean is append-only, so a retry would leave two files and a
    whole-partition read would return every key twice."""
    from ingest.common.config import Settings

    settings = Settings(massive_api_key="k", data_root=tmp_path,
                        log_root=tmp_path / "logs")
    rows = ts.build_rows(_chain_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate)
    ts.write_rows(settings, DAY, rows)
    ts.write_rows(settings, DAY, rows)

    part = tmp_path / "clean" / ts.DATASET / f"dt={DAY.isoformat()}"
    assert len(list(part.glob("*.parquet"))) == 1, "a rerun must not add a file"


def test_replaced_output_is_moved_not_deleted(tmp_path) -> None:
    """The prior file stays recoverable, as on the flat-file path."""
    from ingest.common.config import Settings

    settings = Settings(massive_api_key="k", data_root=tmp_path,
                        log_root=tmp_path / "logs")
    rows = ts.build_rows(_chain_bars(), DAY, roots=("SPXW",), rate_fn=_flat_rate)
    first = ts.write_rows(settings, DAY, rows)
    ts.write_rows(settings, DAY, rows)

    quarantined = list(
        (tmp_path / "_quarantine").rglob(f"{first.name}")
    )
    assert quarantined and quarantined[0].is_file()
