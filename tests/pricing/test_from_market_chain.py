"""Warehouse as-of chain: snapshot + forward → own IV → own Greeks."""

from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from marketdata.opra import parse_opra
from pricing.bsm import price as bsm_price
from pricing.from_market import (
    CHAIN_SCHEMA,
    ChainError,
    greeks_asof,
    main,
    year_fraction,
)
from tests.marketdata.conftest import (
    forward_row,
    partition_path,
    snapshot_row,
    write_records,
)

ET = ZoneInfo("America/New_York")
DT = date(2026, 8, 28)
ASOF = datetime(2026, 8, 28, 15, 0, tzinfo=ET)
ASOF_NS = int(ASOF.timestamp() * 1e9)
ASOF_MS = ASOF_NS // 1_000_000
R = 0.05
SPXW = "O:SPXW260918C07700000"
SPY = "O:SPY260918C00500000"
SIGMA = 0.16
F_SPX = 7700.0
S_SPY = 500.0
EXPIRY = "2026-09-18"


def _spxw_last(asof_ns: int = ASOF_NS, sigma: float = SIGMA) -> float:
    contract = parse_opra(SPXW)
    T = year_fraction(contract, asof_ns)
    S = F_SPX * math.exp(-R * T)
    return float(bsm_price(S, 7700.0, T, R, sigma, "call", q=0.0))


def _spy_last(asof_ns: int = ASOF_NS, sigma: float = SIGMA, q: float = 0.01) -> float:
    contract = parse_opra(SPY)
    T = year_fraction(contract, asof_ns)
    return float(bsm_price(S_SPY, 500.0, T, R, sigma, "call", q=q))


def _spy_forward(asof_ns: int = ASOF_NS, q: float = 0.01) -> float:
    contract = parse_opra(SPY)
    T = year_fraction(contract, asof_ns)
    return S_SPY * math.exp((R - q) * T)


def _spxw_snap(
    last: float,
    *,
    vendor_iv: float | None = 0.25,
    vendor_delta: float | None = 0.55,
) -> dict:
    rec = snapshot_row(
        SPXW,
        strike=7700.0,
        expiry=EXPIRY,
        underlying="SPX",
        cp="call",
        vendor_iv=vendor_iv,
        vendor_delta=vendor_delta,
    )
    rec["underlying_price"] = None
    rec["underlying_ticker"] = "I:SPX"
    rec["last_trade_price"] = last
    rec["day_close"] = last
    rec["greeks_gamma"] = 0.001
    rec["greeks_theta"] = -1.5
    rec["greeks_vega"] = 2.0
    return rec


def _spy_snap(
    last: float,
    ticker: str = SPY,
    *,
    strike: float = 500.0,
    vendor_iv: float | None = 0.25,
    vendor_delta: float | None = 0.55,
) -> dict:
    rec = snapshot_row(
        ticker,
        strike=strike,
        expiry=EXPIRY,
        underlying="SPY",
        cp="call",
        vendor_iv=vendor_iv,
        vendor_delta=vendor_delta,
    )
    rec["underlying_price"] = S_SPY
    rec["last_trade_price"] = last
    rec["day_close"] = last
    return rec


def _write(
    tmp_path: Path,
    *,
    snap: list[dict],
    fwd: list[dict],
    ms: int = ASOF_MS,
    underlying: str = "SPX",
) -> None:
    write_records(
        partition_path(
            tmp_path, "option_snapshots", DT, f"snapshot_sweep-{underlying}-{ms}.parquet"
        ),
        "option_snapshots",
        snap,
    )
    write_records(
        partition_path(
            tmp_path, "forwards", DT, f"snapshot_sweep-{underlying}-{ms}.parquet"
        ),
        "forwards",
        fwd,
    )


def test_happy_path_own_iv_and_greeks(tmp_path: Path) -> None:
    last = _spxw_last()
    _write(
        tmp_path,
        snap=[_spxw_snap(last)],
        fwd=[forward_row(underlying="I:SPX", expiry=EXPIRY, forward=F_SPX, asof_ns=ASOF_NS)],
    )
    table = greeks_asof(
        DT, ASOF_NS, r=R, data_root=tmp_path, roots=("SPXW",), crr_steps=21
    )
    assert table.schema.equals(CHAIN_SCHEMA)
    assert table.num_rows == 1
    row = table.to_pylist()[0]
    assert row["greeks_engine"] == "european_bsm"
    assert row["own_iv"] == pytest.approx(SIGMA, rel=1e-6)
    assert row["own_delta"] is not None and 0.0 < row["own_delta"] < 1.0
    assert row["own_vega"] > 0
    assert math.isfinite(row["own_iv"])
    assert row["price_source"] == "last"
    # Vendor columns are diagnostics: present, but not the sigma we used.
    assert row["vendor_iv"] == pytest.approx(0.25)
    assert row["diff_iv"] == pytest.approx(row["own_iv"] - 0.25)
    assert abs(row["own_iv"] - 0.25) > 0.01


def test_vendor_poison_does_not_change_own_iv(tmp_path: Path) -> None:
    last = _spxw_last()
    _write(
        tmp_path,
        snap=[_spxw_snap(last, vendor_iv=0.12, vendor_delta=0.4)],
        fwd=[forward_row(underlying="I:SPX", expiry=EXPIRY, forward=F_SPX, asof_ns=ASOF_NS)],
    )
    clean = greeks_asof(
        DT, ASOF_NS, r=R, data_root=tmp_path, roots=("SPXW",), crr_steps=21
    ).to_pylist()[0]
    write_records(
        partition_path(tmp_path, "option_snapshots", DT, f"snapshot_sweep-SPX-{ASOF_MS}.parquet"),
        "option_snapshots",
        [_spxw_snap(last, vendor_iv=9.99, vendor_delta=-1.0)],
    )
    poison = greeks_asof(
        DT, ASOF_NS, r=R, data_root=tmp_path, roots=("SPXW",), crr_steps=21
    ).to_pylist()[0]
    assert clean["own_iv"] == pytest.approx(poison["own_iv"], rel=1e-12)
    assert clean["own_delta"] == pytest.approx(poison["own_delta"], rel=1e-12)
    assert poison["vendor_iv"] == pytest.approx(9.99)
    assert poison["diff_iv"] == pytest.approx(poison["own_iv"] - 9.99)


def test_price_outside_bounds_raises(tmp_path: Path) -> None:
    rec = _spxw_snap(1_000_000.0)
    _write(
        tmp_path,
        snap=[rec],
        fwd=[forward_row(underlying="I:SPX", expiry=EXPIRY, forward=F_SPX, asof_ns=ASOF_NS)],
    )
    with pytest.raises(ValueError, match="above max|below intrinsic"):
        greeks_asof(DT, ASOF_NS, r=R, data_root=tmp_path, roots=("SPXW",))


def test_price_outside_bounds_cli_nonzero(tmp_path: Path) -> None:
    _write(
        tmp_path,
        snap=[_spxw_snap(1_000_000.0)],
        fwd=[forward_row(underlying="I:SPX", expiry=EXPIRY, forward=F_SPX, asof_ns=ASOF_NS)],
    )
    rc = main(
        [
            "--date",
            DT.isoformat(),
            "--asof-ns",
            str(ASOF_NS),
            "--r",
            str(R),
            "--roots",
            "SPXW",
            "--data-root",
            str(tmp_path),
        ]
    )
    assert rc == 1


def test_foreign_root_rejected(tmp_path: Path) -> None:
    last = _spy_last()
    spy = _spy_snap(last)
    foreign = snapshot_row("O:SPYL260918C00500000", strike=500.0, expiry=EXPIRY, underlying="SPYL")
    _write(
        tmp_path,
        snap=[spy, foreign],
        fwd=[forward_row(underlying="SPY", expiry=EXPIRY, forward=_spy_forward(), asof_ns=ASOF_NS)],
        underlying="SPY",
    )
    with pytest.raises(ChainError, match="SPYL|ticker_purity|foreign"):
        greeks_asof(DT, ASOF_NS, r=R, data_root=tmp_path, roots=("SPY",), crr_steps=21)


def test_foreign_root_cli_nonzero(tmp_path: Path) -> None:
    last = _spy_last()
    foreign = snapshot_row("O:SPYL260918C00500000", strike=500.0, expiry=EXPIRY, underlying="SPYL")
    _write(
        tmp_path,
        snap=[_spy_snap(last), foreign],
        fwd=[forward_row(underlying="SPY", expiry=EXPIRY, forward=_spy_forward(), asof_ns=ASOF_NS)],
        underlying="SPY",
    )
    rc = main(
        [
            "--date",
            DT.isoformat(),
            "--asof-ns",
            str(ASOF_NS),
            "--r",
            str(R),
            "--roots",
            "SPY",
            "--data-root",
            str(tmp_path),
        ]
    )
    assert rc == 1


def test_asof_picks_last_at_or_before_not_later_file(tmp_path: Path) -> None:
    early_last = _spxw_last(ASOF_NS, sigma=0.16)
    late_asof_ns = (ASOF_MS + 600_000) * 1_000_000
    late_last = _spxw_last(late_asof_ns, sigma=0.35)
    fwd = forward_row(underlying="I:SPX", expiry=EXPIRY, forward=F_SPX, asof_ns=ASOF_NS)
    _write(tmp_path, snap=[_spxw_snap(early_last)], fwd=[fwd], ms=ASOF_MS)
    _write(
        tmp_path,
        snap=[_spxw_snap(late_last, vendor_iv=0.35)],
        fwd=[fwd],
        ms=ASOF_MS + 600_000,
    )
    early = greeks_asof(
        DT, ASOF_NS, r=R, data_root=tmp_path, roots=("SPXW",), crr_steps=21
    ).to_pylist()[0]
    assert early["own_iv"] == pytest.approx(0.16, rel=1e-4)
    assert early["market_price"] == pytest.approx(early_last)
    later = greeks_asof(
        DT, late_asof_ns, r=R, data_root=tmp_path, roots=("SPXW",), crr_steps=21
    ).to_pylist()[0]
    assert later["own_iv"] == pytest.approx(0.35, rel=1e-4)
    assert later["market_price"] == pytest.approx(late_last)


def test_vendor_diffs_populate_when_vendor_cols_exist(tmp_path: Path) -> None:
    last = _spxw_last()
    _write(
        tmp_path,
        snap=[_spxw_snap(last, vendor_iv=0.22, vendor_delta=0.60)],
        fwd=[forward_row(underlying="I:SPX", expiry=EXPIRY, forward=F_SPX, asof_ns=ASOF_NS)],
    )
    row = greeks_asof(
        DT, ASOF_NS, r=R, data_root=tmp_path, roots=("SPXW",), crr_steps=21
    ).to_pylist()[0]
    assert row["vendor_iv"] == pytest.approx(0.22)
    assert row["vendor_delta"] == pytest.approx(0.60)
    assert row["vendor_gamma"] == pytest.approx(0.001)
    assert row["vendor_theta"] == pytest.approx(-1.5)
    assert row["vendor_vega"] == pytest.approx(2.0)
    assert row["diff_iv"] == pytest.approx(row["own_iv"] - 0.22)
    assert row["diff_delta"] == pytest.approx(row["own_delta"] - 0.60)
    assert row["diff_gamma"] == pytest.approx(row["own_gamma"] - 0.001)
    assert row["diff_theta"] == pytest.approx(row["own_theta"] - (-1.5))
    assert row["diff_vega"] == pytest.approx(row["own_vega"] - 2.0)


def test_vendor_diffs_null_when_vendor_cols_null(tmp_path: Path) -> None:
    last = _spxw_last()
    rec = _spxw_snap(last, vendor_iv=None, vendor_delta=None)
    rec["greeks_gamma"] = None
    rec["greeks_theta"] = None
    rec["greeks_vega"] = None
    rec["implied_volatility"] = None
    _write(
        tmp_path,
        snap=[rec],
        fwd=[forward_row(underlying="I:SPX", expiry=EXPIRY, forward=F_SPX, asof_ns=ASOF_NS)],
    )
    row = greeks_asof(
        DT, ASOF_NS, r=R, data_root=tmp_path, roots=("SPXW",), crr_steps=21
    ).to_pylist()[0]
    assert row["own_iv"] == pytest.approx(SIGMA, rel=1e-6)
    assert row["vendor_iv"] is None
    assert row["diff_iv"] is None
    assert row["diff_delta"] is None


def test_spy_uses_american_crr(tmp_path: Path) -> None:
    last = _spy_last()
    _write(
        tmp_path,
        snap=[_spy_snap(last)],
        fwd=[forward_row(underlying="SPY", expiry=EXPIRY, forward=_spy_forward(), asof_ns=ASOF_NS)],
        underlying="SPY",
    )
    row = greeks_asof(
        DT, ASOF_NS, r=R, data_root=tmp_path, roots=("SPY",), crr_steps=21
    ).to_pylist()[0]
    assert row["greeks_engine"] == "american_crr"
    assert row["exercise_style"] == "american"
    assert 0.0 < row["own_delta"] < 1.0
    assert row["own_vega"] > 0
    assert math.isfinite(row["own_iv"])


def test_spy_atm_subset_is_explicit(tmp_path: Path) -> None:
    """Far SPY strikes are European when --spy-atm-pct is set; ATM stays CRR."""
    last_atm = _spy_last()
    far = "O:SPY260918C00540000"
    rec_far = _spy_snap(last_atm, ticker=far, strike=540.0)
    contract = parse_opra(far)
    T = year_fraction(contract, ASOF_NS)
    rec_far["last_trade_price"] = float(bsm_price(S_SPY, 540.0, T, R, SIGMA, "call", q=0.01))
    rec_far["day_close"] = rec_far["last_trade_price"]
    _write(
        tmp_path,
        snap=[_spy_snap(last_atm), rec_far],
        fwd=[forward_row(underlying="SPY", expiry=EXPIRY, forward=_spy_forward(), asof_ns=ASOF_NS)],
        underlying="SPY",
    )
    table = greeks_asof(
        DT,
        ASOF_NS,
        r=R,
        data_root=tmp_path,
        roots=("SPY",),
        crr_steps=21,
        spy_american_moneyness=0.05,
    )
    by_ticker = {r["ticker"]: r for r in table.to_pylist()}
    assert by_ticker[SPY]["greeks_engine"] == "american_crr"
    assert by_ticker[far]["greeks_engine"] == "european_bsm"


def test_empty_allowlisted_root_fails(tmp_path: Path) -> None:
    last = _spy_last()
    _write(
        tmp_path,
        snap=[_spy_snap(last)],
        fwd=[forward_row(underlying="SPY", expiry=EXPIRY, forward=_spy_forward(), asof_ns=ASOF_NS)],
        underlying="SPY",
    )
    with pytest.raises(ChainError, match="empty allowlisted root|root\\[SPXW\\]"):
        greeks_asof(DT, ASOF_NS, r=R, data_root=tmp_path, roots=("SPY", "SPXW"), crr_steps=21)


def test_mixed_underlyings_in_spy_only_query_fail(tmp_path: Path) -> None:
    last_spy = _spy_last()
    last_spxw = _spxw_last()
    write_records(
        partition_path(tmp_path, "option_snapshots", DT, f"snapshot_sweep-SPY-{ASOF_MS}.parquet"),
        "option_snapshots",
        [_spy_snap(last_spy)],
    )
    write_records(
        partition_path(tmp_path, "option_snapshots", DT, f"snapshot_sweep-SPX-{ASOF_MS}.parquet"),
        "option_snapshots",
        [_spxw_snap(last_spxw)],
    )
    write_records(
        partition_path(tmp_path, "forwards", DT, f"snapshot_sweep-SPY-{ASOF_MS}.parquet"),
        "forwards",
        [forward_row(underlying="SPY", expiry=EXPIRY, forward=_spy_forward(), asof_ns=ASOF_NS)],
    )
    write_records(
        partition_path(tmp_path, "forwards", DT, f"snapshot_sweep-SPX-{ASOF_MS}.parquet"),
        "forwards",
        [forward_row(underlying="I:SPX", expiry=EXPIRY, forward=F_SPX, asof_ns=ASOF_NS)],
    )
    with pytest.raises(ChainError, match="foreign|ticker_purity|SPXW"):
        greeks_asof(DT, ASOF_NS, r=R, data_root=tmp_path, roots=("SPY",), crr_steps=21)


def test_extra_schema_column_is_error(tmp_path: Path) -> None:
    last = _spxw_last()
    path = partition_path(tmp_path, "option_snapshots", DT, f"snapshot_sweep-SPX-{ASOF_MS}.parquet")
    write_records(path, "option_snapshots", [_spxw_snap(last)], extra={"bonus": [1.0]})
    write_records(
        partition_path(tmp_path, "forwards", DT, f"snapshot_sweep-SPX-{ASOF_MS}.parquet"),
        "forwards",
        [forward_row(underlying="I:SPX", expiry=EXPIRY, forward=F_SPX, asof_ns=ASOF_NS)],
    )
    with pytest.raises(Exception, match="extra columns"):
        greeks_asof(DT, ASOF_NS, r=R, data_root=tmp_path, roots=("SPXW",))


def test_missing_forward_fails_loud(tmp_path: Path) -> None:
    last = _spxw_last()
    _write(
        tmp_path,
        snap=[_spxw_snap(last)],
        fwd=[forward_row(underlying="I:SPX", expiry="2026-12-18", forward=F_SPX, asof_ns=ASOF_NS)],
    )
    with pytest.raises(ChainError, match="no forward"):
        greeks_asof(DT, ASOF_NS, r=R, data_root=tmp_path, roots=("SPXW",))


def test_cli_happy_path(tmp_path: Path) -> None:
    last = _spxw_last()
    _write(
        tmp_path,
        snap=[_spxw_snap(last)],
        fwd=[forward_row(underlying="I:SPX", expiry=EXPIRY, forward=F_SPX, asof_ns=ASOF_NS)],
    )
    out = tmp_path / "greeks.parquet"
    rc = main(
        [
            "--date",
            DT.isoformat(),
            "--asof-ns",
            str(ASOF_NS),
            "--r",
            str(R),
            "--roots",
            "SPXW",
            "--data-root",
            str(tmp_path),
            "--output",
            str(out),
            "--crr-steps",
            "21",
        ]
    )
    assert rc == 0
    assert out.is_file()
