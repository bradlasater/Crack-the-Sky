"""Tests for per-underlying reference prices and watchlist selection.

The regression these lock down: a single SPY-derived strike band was applied
to every contract, so SPX (which trades near 10x SPY) fell outside it and the
job polled 2 of 28,648 SPX contracts while logging a healthy-looking run.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from ingest import schemas
from ingest.common import landing
from ingest.common.config import Settings
from ingest.jobs import (
    OPTION_ROOTS,
    compute_watchlist,
    forward_from_parity,
    keep_ticker,
    latest_contracts,
    reference_price,
    ticker_root,
    underlying_root,
)

RUN_DATE = date(2026, 8, 31)
SPY_SPOT = 767.38
SPX_SPOT = 7691.0


def _settings(data_root: Path) -> Settings:
    return Settings(
        massive_api_key="test-key",
        data_root=data_root,
        log_root=data_root / "logs",
    )


def _opra(root: str, exp: date, kind: str, strike: float) -> str:
    return (
        f"O:{root}{exp.strftime('%y%m%d')}"
        f"{'C' if kind == 'call' else 'P'}{int(strike * 1000):08d}"
    )


def _contract(root: str, underlying: str, exp: date, kind: str, strike: float) -> dict:
    return schemas.contract_record({
        "ticker": _opra(root, exp, kind, strike),
        "underlying_ticker": underlying,
        "contract_type": kind,
        "exercise_style": "american",
        "expiration_date": exp.isoformat(),
        "strike_price": strike,
        "shares_per_contract": 100,
    })


def _snapshot(root: str, underlying: str, exp: date, kind: str, strike: float,
              spot: float | None, volume: float, oi: float) -> dict:
    """Snapshot record priced by intrinsic value, so parity recovers ``spot``."""
    intrinsic = max(spot - strike, 0.0) if kind == "call" else max(strike - spot, 0.0)
    return {
        "ticker": _opra(root, exp, kind, strike),
        "details_contract_type": kind,
        "details_expiration_date": exp.isoformat(),
        "details_strike_price": strike,
        "details_shares_per_contract": 100,
        "day_close": intrinsic,
        "day_volume": volume,
        "day_last_updated_ns": 1788212108195000000,
        "open_interest": oi,
        "underlying_ticker": underlying,
        # I:SPX carries no underlying_price on this tier -- that is the whole
        # reason parity exists.
        "underlying_price": spot if underlying == "SPY" else None,
    }


@pytest.fixture()
def landed(tmp_path: Path) -> Settings:
    """A contracts + option_snapshots partition covering SPY and SPX."""
    settings = _settings(tmp_path)
    expiries = [RUN_DATE + timedelta(days=d) for d in (14, 21, 30)]
    contracts, snaps_spy, snaps_spx = [], [], []

    for exp in expiries:
        for offset in range(-40, 41, 5):          # +/- 5.2% around spot
            strike = round(SPY_SPOT + offset, 0)
            for kind in ("call", "put"):
                contracts.append(_contract("SPY", "SPY", exp, kind, strike))
                snaps_spy.append(_snapshot("SPY", "SPY", exp, kind, strike,
                                           SPY_SPOT, volume=5, oi=500))
        for offset in range(-400, 401, 50):
            strike = round(SPX_SPOT + offset, 0)
            for kind in ("call", "put"):
                # SPXW is ~98% of real SPX option activity.
                contracts.append(_contract("SPXW", "SPX", exp, kind, strike))
                snaps_spx.append(_snapshot("SPXW", "SPX", exp, kind, strike,
                                           SPX_SPOT, volume=5, oi=500))

    landing.write_clean("contracts", RUN_DATE, contracts, job="contracts_sync-SPY",
                        data_root=tmp_path)
    landing.write_clean("option_snapshots", RUN_DATE, snaps_spy,
                        job="snapshot_sweep-SPY", data_root=tmp_path)
    landing.write_clean("option_snapshots", RUN_DATE, snaps_spx,
                        job="snapshot_sweep-I:SPX", data_root=tmp_path)
    return settings


# ---------------------------------------------------------------------------
# Ticker roots
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("O:SPY260918C00770000", True),
        ("O:SPX260918C08000000", True),
        ("O:SPXW260918P07600000", True),
        # Leveraged ETFs a bare startswith("O:SPX"/"O:SPY") prefix admitted.
        ("O:SPXL260918C00250000", False),
        ("O:SPXS260918P00010000", False),
        ("O:SPXU260918C00020000", False),
        ("O:SPYG260918C00100000", False),
        ("O:QQQ260918C00500000", False),
        ("", False),
    ],
)
def test_keep_ticker_matches_root_not_prefix(ticker: str, expected: bool) -> None:
    assert keep_ticker(ticker) is expected


def test_ticker_root_extracts_root() -> None:
    assert ticker_root("O:SPXW260918P07600000") == "SPXW"
    assert ticker_root("O:SPY260918C00770000") == "SPY"
    assert ticker_root("not-a-ticker") is None


def test_underlying_root_normalises_index_prefix() -> None:
    assert underlying_root("I:SPX") == "SPX"
    assert underlying_root("SPX") == "SPX"
    assert underlying_root("SPY") == "SPY"


def test_spxw_is_an_expected_root() -> None:
    # SPXW carries ~98% of SPX option trades; excluding it empties the SPX side.
    assert "SPXW" in OPTION_ROOTS


# ---------------------------------------------------------------------------
# Parity forwards
# ---------------------------------------------------------------------------

def test_forward_from_parity_recovers_spot(landed: Settings) -> None:
    from ingest.jobs import latest_snapshots

    snaps = latest_snapshots(landed, RUN_DATE)
    forwards = forward_from_parity(snaps["SPX"])
    assert forwards, "expected one forward per expiry"
    for f in forwards:
        # Options priced at intrinsic => K + C - P == spot exactly.
        assert f["forward"] == pytest.approx(SPX_SPOT, abs=1e-6)
        assert f["method"] == "parity"
        assert f["pairs"] > 0


def test_forward_from_parity_ignores_unpaired_strikes() -> None:
    exp = RUN_DATE + timedelta(days=14)
    records = [
        _snapshot("SPXW", "SPX", exp, "call", 7700.0, SPX_SPOT, 1, 1),
        # A put with no matching call must not produce a forward on its own.
        _snapshot("SPXW", "SPX", exp, "put", 7000.0, SPX_SPOT, 1, 1),
    ]
    assert forward_from_parity(records) == []


def test_reference_price_uses_spot_for_spy_and_parity_for_spx(landed: Settings) -> None:
    from ingest.jobs import latest_snapshots

    snaps = latest_snapshots(landed, RUN_DATE)
    spy_ref, spy_method = reference_price("SPY", snaps["SPY"])
    spx_ref, spx_method = reference_price("SPX", snaps["SPX"], spy_ref)
    assert (spy_ref, spy_method) == (pytest.approx(SPY_SPOT), "spot")
    assert spx_method == "parity"
    assert spx_ref == pytest.approx(SPX_SPOT, abs=1e-6)


def test_reference_price_falls_back_to_spy_proxy() -> None:
    ref, method = reference_price("SPX", [], spy_reference=SPY_SPOT)
    assert method == "proxy"
    assert ref == pytest.approx(SPY_SPOT * 10)


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def test_watchlist_includes_spx_not_just_spy(landed: Settings) -> None:
    """The regression: SPX must not be filtered out by a SPY-derived band."""
    watchlist = compute_watchlist(landed, RUN_DATE)
    roots = {}
    for rec in watchlist:
        roots[ticker_root(rec["ticker"])] = roots.get(ticker_root(rec["ticker"]), 0) + 1
    assert roots.get("SPY", 0) > 0
    assert roots.get("SPXW", 0) > 0, "SPX/SPXW must appear in the watchlist"
    # Both sides are fully in-band here, so neither may dominate by ~1000x the
    # way the single-band bug produced (2,658 SPY vs 2 SPX).
    assert roots["SPXW"] > roots["SPY"] / 10


def test_watchlist_bands_are_per_underlying(landed: Settings) -> None:
    """Every selected strike sits within +/-15% of its own underlying."""
    for rec in compute_watchlist(landed, RUN_DATE):
        ref = SPY_SPOT if rec["underlying_ticker"] == "SPY" else SPX_SPOT
        assert 0.85 * ref <= rec["strike_price"] <= 1.15 * ref


def test_watchlist_liquidity_filter_drops_dead_strikes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    exp = RUN_DATE + timedelta(days=14)
    contracts = [_contract("SPY", "SPY", exp, k, s)
                 for s in (760.0, 770.0) for k in ("call", "put")]
    snaps = [
        _snapshot("SPY", "SPY", exp, k, 760.0, SPY_SPOT, volume=0, oi=0)
        for k in ("call", "put")
    ] + [
        _snapshot("SPY", "SPY", exp, k, 770.0, SPY_SPOT, volume=25, oi=900)
        for k in ("call", "put")
    ]
    landing.write_clean("contracts", RUN_DATE, contracts,
                        job="contracts_sync-SPY", data_root=tmp_path)
    landing.write_clean("option_snapshots", RUN_DATE, snaps,
                        job="snapshot_sweep-SPY", data_root=tmp_path)

    kept = {r["strike_price"] for r in compute_watchlist(settings, RUN_DATE)}
    assert kept == {770.0}, "illiquid strike should be dropped"

    unfiltered = {r["strike_price"]
                  for r in compute_watchlist(settings, RUN_DATE, require_liquidity=False)}
    assert unfiltered == {760.0, 770.0}


def test_watchlist_raises_without_contracts(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="contracts_sync"):
        compute_watchlist(_settings(tmp_path), RUN_DATE)


# ---------------------------------------------------------------------------
# Partition readers
# ---------------------------------------------------------------------------

def test_latest_contracts_deduplicates_repeat_syncs(tmp_path: Path) -> None:
    """contracts_sync runs twice daily; the universe must not double."""
    settings = _settings(tmp_path)
    exp = RUN_DATE + timedelta(days=14)
    records = [_contract("SPY", "SPY", exp, "call", 770.0)]
    for _ in range(2):  # 08:00 and 16:30 syncs land separate files
        landing.write_clean("contracts", RUN_DATE, records,
                            job="contracts_sync-SPY", data_root=tmp_path)
        time.sleep(0.002)  # distinct epoch-ms filenames
    part = tmp_path / "clean" / "contracts" / f"dt={RUN_DATE.isoformat()}"
    assert len(list(part.glob("*.parquet"))) == 2, "need two files to dedupe"
    assert len(latest_contracts(settings, RUN_DATE)) == 1


def test_latest_contracts_includes_unrecognised_filenames(tmp_path: Path) -> None:
    """An imported partition must never be silently ignored."""
    settings = _settings(tmp_path)
    exp = RUN_DATE + timedelta(days=14)
    landing.write_clean("contracts", RUN_DATE,
                        [_contract("SPY", "SPY", exp, "call", 770.0)],
                        job="imported", data_root=tmp_path)
    assert len(latest_contracts(settings, RUN_DATE)) == 1


# ---------------------------------------------------------------------------
# The as-of datasets must stay readable through latest_clean_records
# ---------------------------------------------------------------------------

def test_latest_clean_records_reads_asof_datasets(tmp_path: Path) -> None:
    """contracts_sync._previous_tickers depends on this.

    read_partition is fail-loud for multi-write datasets, but this accessor is
    the "current state" reader: on a fresh run the SPY pass writes the first
    file, so the SPX pass would otherwise hit its own partition and raise.
    """
    from ingest.jobs import latest_clean_records

    settings = _settings(tmp_path)
    exp = RUN_DATE + timedelta(days=14)
    landing.write_clean("contracts", RUN_DATE,
                        [_contract("SPY", "SPY", exp, "call", 770.0)],
                        job="contracts_sync-SPY", data_root=tmp_path)
    # Second underlying lands into the same partition, as the real job does.
    landing.write_clean("contracts", RUN_DATE,
                        [_contract("SPXW", "SPX", exp, "put", 7600.0)],
                        job="contracts_sync-I:SPX", data_root=tmp_path)

    rows = latest_clean_records(settings, "contracts", RUN_DATE)
    assert {r["underlying_ticker"] for r in rows} == {"SPY", "SPX"}


def test_latest_clean_records_deduplicates_repeat_asof_writes(tmp_path: Path) -> None:
    """contracts_sync runs three times a day; the universe must not multiply."""
    import time as _time

    from ingest.jobs import latest_clean_records

    settings = _settings(tmp_path)
    exp = RUN_DATE + timedelta(days=14)
    for _ in range(3):
        landing.write_clean("contracts", RUN_DATE,
                            [_contract("SPY", "SPY", exp, "call", 770.0)],
                            job="contracts_sync-SPY", data_root=tmp_path)
        _time.sleep(0.002)
    assert len(latest_clean_records(settings, "contracts", RUN_DATE)) == 1


def test_latest_clean_records_still_whole_partition_for_normal_datasets(
    tmp_path: Path,
) -> None:
    from ingest.jobs import latest_clean_records

    settings = _settings(tmp_path)
    rows = [{"ticker": "SPY", "start_ms": i, "open": 1.0, "high": 1.0,
             "low": 1.0, "close": 1.0, "volume": 1.0, "vwap": 1.0,
             "transactions": 1} for i in range(3)]
    landing.write_clean("underlying_minute_bars", RUN_DATE, rows,
                        job="underlying_bars", data_root=tmp_path)
    assert len(latest_clean_records(settings, "underlying_minute_bars", RUN_DATE)) == 3
