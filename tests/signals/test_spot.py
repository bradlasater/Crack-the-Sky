"""spy_spot: bar close wins; parity proxy fills holes; resid only on overlap."""

from __future__ import annotations

import math
from datetime import date

import pytest

from ingest.common import landing
from ingest.common.config import Settings
from ingest.schemas import SCHEMAS
from signals.spot import (
    ROLL_DEBIAS_FRAC,
    SRC_BARS,
    SRC_PARITY,
    assemble,
    build_for_date,
    calibrate,
    proxy_spot,
    pv_dividends,
    shortest_spy_term,
    spy_close,
    write_row,
)

SESSION = date(2026, 9, 2)
EXPIRY = date(2026, 9, 3)
DTE = 1
T = DTE / 365.0
R = 0.04
CLOSE = 765.16
F = 765.51


def _term(**over: object) -> dict:
    row = {
        "underlying": "SPY",
        "expiration_date": EXPIRY.isoformat(),
        "dte": DTE,
        "t_years": T,
        "forward": F,
        "rate": R,
    }
    row.update(over)
    return row


def _div(ex: str, cash: float) -> dict:
    return {
        "ticker": "SPY",
        "cash_amount": cash,
        "ex_dividend_date": ex,
        "dividend_type": "CD",
    }


def test_schema_has_the_documented_columns() -> None:
    names = [f.name for f in SCHEMAS["spy_spot"]]
    assert names == [
        "date", "spot", "forward", "dte", "rate", "q", "src", "resid",
    ]


def test_spy_close_picks_spy_and_ignores_vix_proxies() -> None:
    rows = [
        {"ticker": "VIXY", "close": 12.0},
        {"ticker": "SPY", "close": CLOSE},
        {"ticker": "UVXY", "close": 8.0},
    ]
    assert spy_close(rows) == pytest.approx(CLOSE)
    assert spy_close([{"ticker": "VIXY", "close": 12.0}]) is None
    assert spy_close([{"ticker": "SPY", "close": 0.0}]) is None


def test_shortest_spy_term_is_positive_dte_spy_only() -> None:
    rows = [
        _term(underlying="SPXW", dte=1, forward=7700.0),
        _term(dte=7, forward=766.0),
        _term(dte=1, forward=F),
        _term(dte=0, forward=764.0),
        _term(underlying="SPY", dte=2, forward=None),
    ]
    got = shortest_spy_term(rows)
    assert got is not None
    assert got["dte"] == 1
    assert got["forward"] == pytest.approx(F)
    assert shortest_spy_term([_term(dte=0)]) is None


def test_proxy_without_a_dividend_is_discounted_forward() -> None:
    spot, q = proxy_spot(_term(), [], SESSION)
    expected = F * math.exp(-R * T)
    assert spot == pytest.approx(expected)
    assert q == pytest.approx(0.0, abs=1e-12)
    reconstructed = spot * math.exp((R - q) * T)
    assert reconstructed == pytest.approx(F)


def test_proxy_adds_pv_of_dividends_inside_the_window() -> None:
    inside = _div("2026-09-03", 1.50)
    outside = _div("2026-09-04", 9.99)  # after expiry
    before = _div("2026-09-02", 9.99)  # session close does not earn this
    spot0, _ = proxy_spot(_term(), [], SESSION)
    spot, q = proxy_spot(_term(), [inside, outside, before], SESSION)
    t_ex = 1 / 365.0
    expected = spot0 + 1.50 * math.exp(-R * t_ex)
    assert spot == pytest.approx(expected)
    assert q == pytest.approx(R - math.log(F / spot) / T)
    assert spot > spot0
    reconstructed = spot * math.exp((R - q) * T)
    assert reconstructed == pytest.approx(F, rel=1e-12)


def test_pv_dividends_skips_non_positive_cash() -> None:
    assert pv_dividends([_div("2026-09-03", 0.0)], SESSION, EXPIRY, R) == 0.0
    assert pv_dividends([_div("2026-09-03", -1.0)], SESSION, EXPIRY, R) == 0.0


def test_bars_win_and_resid_is_proxy_minus_actual() -> None:
    row = assemble(SESSION, close=CLOSE, term=_term(), dividends=[])
    assert row is not None
    assert row["src"] == SRC_BARS
    assert row["spot"] == pytest.approx(CLOSE)
    proxy, q = proxy_spot(_term(), [], SESSION)
    assert row["resid"] == pytest.approx(proxy - CLOSE)
    assert row["q"] == pytest.approx(q)
    assert row["forward"] == pytest.approx(F)
    assert row["dte"] == DTE
    assert row["date"] == "2026-09-02"


def test_parity_fill_when_no_bar() -> None:
    row = assemble(SESSION, close=None, term=_term(), dividends=[])
    assert row is not None
    assert row["src"] == SRC_PARITY
    proxy, _ = proxy_spot(_term(), [], SESSION)
    assert row["spot"] == pytest.approx(proxy)
    assert row["resid"] is None


def test_bars_only_leaves_proxy_columns_null() -> None:
    row = assemble(SESSION, close=CLOSE, term=None, dividends=[])
    assert row is not None
    assert row["src"] == SRC_BARS
    assert row["spot"] == pytest.approx(CLOSE)
    assert row["resid"] is None
    assert row["forward"] is None
    assert row["dte"] is None
    assert row["rate"] is None
    assert row["q"] is None


def test_neither_source_is_none() -> None:
    assert assemble(SESSION, close=None, term=None, dividends=[]) is None


def test_calibrate_reports_overlap_and_roll_threshold() -> None:
    rows = []
    for i, (spot, proxy) in enumerate(
        [(100.0, 100.2), (101.0, 101.1), (103.0, 103.4), (102.0, 102.0)]
    ):
        rows.append({
            "date": f"2026-09-0{i + 1}",
            "spot": spot,
            "src": SRC_BARS,
            "resid": proxy - spot,
        })
    rows.append({
        "date": "2026-08-01", "spot": 90.0, "src": SRC_PARITY, "resid": None,
    })
    report = calibrate(rows)
    assert report["n_rows"] == 5
    assert report["n_bars"] == 4
    assert report["n_parity"] == 1
    assert report["n_overlap"] == 4
    abs_err = sorted([0.2, 0.1, 0.4, 0.0])
    assert report["median_abs_error"] == pytest.approx(abs_err[len(abs_err) // 2])
    assert report["max_abs_error"] == pytest.approx(0.4)
    assert report["median_abs_daily_move"] == pytest.approx(1.0)  # 1, 2, 1
    assert report["error_vs_move"] == pytest.approx(report["median_abs_error"] / 1.0)
    assert report["roll_debias_required"] is (
        report["error_vs_move"] >= ROLL_DEBIAS_FRAC
    )
    assert report["autocorr_lag1"] is not None


def test_calibrate_empty() -> None:
    report = calibrate([])
    assert report["n_overlap"] == 0
    assert report["median_abs_error"] is None
    assert report["roll_debias_required"] is False


def test_write_row_projects_the_schema(tmp_path) -> None:
    settings = Settings(massive_api_key="k", data_root=tmp_path)
    row = assemble(SESSION, close=CLOSE, term=_term(), dividends=[])
    assert row is not None
    path = write_row(settings, SESSION, row)
    assert path.is_file()
    from marketdata.catalog import read_partition

    got = read_partition("spy_spot", SESSION, tmp_path).to_pylist()[0]
    assert got["src"] == SRC_BARS
    assert got["spot"] == pytest.approx(CLOSE)
    assert set(got) == {f.name for f in SCHEMAS["spy_spot"]}


def test_write_row_replaces_prior_output(tmp_path) -> None:
    settings = Settings(massive_api_key="k", data_root=tmp_path)
    row = assemble(SESSION, close=CLOSE, term=_term(), dividends=[])
    assert row is not None
    write_row(settings, SESSION, row)
    row2 = dict(row)
    row2["spot"] = 1.0
    write_row(settings, SESSION, row2)
    from marketdata.catalog import read_partition

    got = read_partition("spy_spot", SESSION, tmp_path).to_pylist()
    assert len(got) == 1
    assert got[0]["spot"] == pytest.approx(1.0)


def test_build_for_date_reads_the_three_sources(tmp_path) -> None:
    settings = Settings(massive_api_key="k", data_root=tmp_path)
    landing.write_clean(
        "underlying_day_bars",
        SESSION,
        [{
            "ticker": "SPY", "start_ms": 1, "open": CLOSE, "high": CLOSE,
            "low": CLOSE, "close": CLOSE, "volume": 1.0, "vwap": CLOSE,
            "transactions": 1,
        }],
        job="grouped_daily",
        data_root=tmp_path,
    )
    landing.write_clean(
        "atm_term_structure",
        SESSION,
        [{
            "date": SESSION.isoformat(), "underlying": "SPY",
            "expiration_date": EXPIRY.isoformat(), "dte": DTE, "t_years": T,
            "forward": F, "atm_strike": F, "call_price": 1.0, "put_price": 1.0,
            "call_iv": 0.1, "put_iv": 0.1, "atm_iv": 0.1, "rate": R,
            "pairs": 3, "method": "parity", "src": "day_bars",
        }],
        job="term_structure",
        data_root=tmp_path,
    )
    landing.write_clean(
        "dividends",
        SESSION,
        [_div("2026-06-18", 1.65)],
        job="dividends_sync-SPY",
        data_root=tmp_path,
    )
    row = build_for_date(settings, SESSION)
    assert row is not None
    assert row["src"] == SRC_BARS
    assert row["spot"] == pytest.approx(CLOSE)
    assert row["resid"] is not None
