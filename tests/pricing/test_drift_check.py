"""Daily identity canary: own math first; vendor diffs diagnostic when present."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pytest

from ingest.common import landing
from pricing import drift_check as drift_mod
from pricing.bsm import price as bsm_price
from pricing.bsm import raw_greeks
from pricing.conventions import CALENDAR_DAYS_PER_YEAR
from pricing.drift_check import (
    DEFAULT_THRESHOLDS,
    VENDOR_THETA_TO_YEAR,
    VENDOR_VEGA_TO_PER_1,
    DriftReport,
    Thresholds,
    align_vendor,
    cutoff_asof_ns,
    evaluate_drift,
    main,
    run_drift,
)
from pricing.from_market import ChainCounts, greeks_asof
from tests.marketdata.conftest import forward_row
from tests.pricing.test_from_market_chain import (
    ASOF_NS,
    DT,
    EXPIRY,
    F_SPX,
    SPXW,
    R,
    _spxw_last,
    _spxw_snap,
    _write,
)

LOOSE = Thresholds(min_compare=1, fail_frac=0.25, iv_median_abs=0.04, atm_pct=0.05)
ET = ZoneInfo("America/New_York")
SPXW_PUT = "O:SPXW260918P07700000"

_CLI = [
    "--date",
    DT.isoformat(),
    "--asof-ns",
    str(ASOF_NS),
    "--r",
    str(R),
    "--roots",
    "SPXW",
    "--min-compare",
    "1",
    "--crr-steps",
    "21",
    "--force",
]


def _vendor_matching_own(snap: dict, row: dict) -> dict:
    """Vendor snapshot units: theta per day, vega per 1% — inverse of align_vendor."""
    out = dict(snap)
    out["implied_volatility"] = row["own_iv"]
    out["greeks_delta"] = row["own_delta"]
    out["greeks_gamma"] = row["own_gamma"]
    out["greeks_theta"] = row["own_theta"] / VENDOR_THETA_TO_YEAR
    out["greeks_vega"] = row["own_vega"] / VENDOR_VEGA_TO_PER_1
    return out


def _null_vendor(snap: dict) -> dict:
    out = dict(snap)
    out["implied_volatility"] = None
    out["greeks_delta"] = None
    out["greeks_gamma"] = None
    out["greeks_theta"] = None
    out["greeks_vega"] = None
    return out


def _aligned_warehouse(tmp_path: Path, *, poison_iv: float | None = None) -> dict:
    last = _spxw_last()
    snap = _spxw_snap(last)
    fwd = [forward_row(underlying="I:SPX", expiry=EXPIRY, forward=F_SPX, asof_ns=ASOF_NS)]
    _write(tmp_path, snap=[snap], fwd=fwd)
    row = greeks_asof(
        DT, ASOF_NS, r=R, data_root=tmp_path, roots=("SPXW",), crr_steps=21
    ).to_pylist()[0]
    aligned = _vendor_matching_own(snap, row)
    if poison_iv is not None:
        aligned["implied_volatility"] = poison_iv
        aligned["greeks_delta"] = 0.05
    _write(tmp_path, snap=[aligned], fwd=fwd)
    return row


def _null_vendor_warehouse(tmp_path: Path) -> None:
    last = _spxw_last()
    snap = _null_vendor(_spxw_snap(last, vendor_iv=None, vendor_delta=None))
    fwd = [forward_row(underlying="I:SPX", expiry=EXPIRY, forward=F_SPX, asof_ns=ASOF_NS)]
    _write(tmp_path, snap=[snap], fwd=fwd)


def _consistent_euro_row(
    *,
    ticker: str,
    cp: str,
    poison_gamma: float | None = None,
    poison_market: float | None = None,
) -> dict:
    S, K, T, rate, q, sig = 7700.0, 7700.0, 0.05, 0.04, 0.0, 0.16
    g = raw_greeks(S, K, T, rate, sig, cp, q=q)
    mkt = float(bsm_price(S, K, T, rate, sig, cp, q=q))
    return {
        "ticker": ticker,
        "root": "SPXW",
        "expiry": EXPIRY,
        "call_put": cp,
        "strike": K,
        "F": K,
        "S": S,
        "T": T,
        "r": rate,
        "q": q,
        "exercise_style": "european",
        "greeks_engine": "european_bsm",
        "own_iv": sig,
        "own_delta": float(g["delta"]),
        "own_gamma": poison_gamma if poison_gamma is not None else float(g["gamma"]),
        "own_vega": float(g["vega"]),
        "own_theta": float(g["theta"]),
        "market_price": poison_market if poison_market is not None else mkt,
        "vendor_iv": None,
        "vendor_delta": None,
        "vendor_gamma": None,
        "vendor_vega": None,
        "vendor_theta": None,
    }


def test_vendor_theta_vega_scales_are_the_documented_conversions() -> None:
    assert VENDOR_THETA_TO_YEAR == float(CALENDAR_DAYS_PER_YEAR) == 365.0
    assert VENDOR_VEGA_TO_PER_1 == 100.0
    row = {
        "own_theta": -365.0,
        "own_vega": 20.0,
        "vendor_theta": -1.0,
        "vendor_vega": 0.20,
        "vendor_iv": 0.16,
        "vendor_delta": 0.5,
        "vendor_gamma": 0.01,
    }
    aligned = align_vendor(row)
    assert aligned["theta"] == pytest.approx(-365.0)
    assert aligned["vega"] == pytest.approx(20.0)
    assert abs(row["own_theta"] - row["vendor_theta"]) > 300


def test_aligned_units_within_band_pass(tmp_path: Path) -> None:
    _aligned_warehouse(tmp_path)
    report = run_drift(
        DT,
        r=R,
        data_root=tmp_path,
        asof_ns=ASOF_NS,
        roots=("SPXW",),
        crr_steps=21,
        spy_atm_pct=0.05,
        atm_pct=0.05,
        max_rows=50,
        uninvertible="skip",
        thresholds=LOOSE,
    )
    assert report.status == "PASS"
    assert report.failures == []
    assert report.counts["atm_compared"] == 1
    assert report.vendor_compare_skipped is False
    assert report.median_abs_iv is not None
    assert report.median_abs_iv < LOOSE.iv_median_abs
    assert report.median_abs_reprice is not None
    assert report.median_abs_reprice < LOOSE.reprice_median_abs


def test_poisoned_vendor_divergence_fails(tmp_path: Path) -> None:
    _aligned_warehouse(tmp_path, poison_iv=0.99)
    report = run_drift(
        DT,
        r=R,
        data_root=tmp_path,
        asof_ns=ASOF_NS,
        roots=("SPXW",),
        crr_steps=21,
        max_rows=50,
        uninvertible="skip",
        thresholds=LOOSE,
    )
    assert report.status == "FAIL"
    assert report.vendor_compare_skipped is False
    assert report.median_abs_iv is not None
    assert report.median_abs_iv > LOOSE.iv_median_abs
    assert any("ΔIV" in f or "beyond band" in f for f in report.failures)


def test_null_vendor_iv_identities_hold_exit_0(tmp_path: Path) -> None:
    _null_vendor_warehouse(tmp_path)
    rc = main([*_CLI, "--data-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(
        landing.meta_path("drift_check.json", data_root=tmp_path).read_text(encoding="utf-8")
    )
    assert payload["status"] == "PASS"
    assert payload["vendor_compare_skipped"] is True
    assert payload["failures"] == []


def test_identities_broken_poisoned_price_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _null_vendor_warehouse(tmp_path)
    real = drift_mod.greeks_asof

    def poison(*args: object, **kwargs: object) -> pa.Table:
        table = real(*args, **kwargs)
        rows = table.to_pylist()
        rows[0]["market_price"] = float(rows[0]["market_price"]) + 50.0
        return pa.Table.from_pylist(rows, schema=table.schema)

    monkeypatch.setattr(drift_mod, "greeks_asof", poison)
    rc = main([*_CLI, "--data-root", str(tmp_path)])
    assert rc == 1
    payload = json.loads(
        landing.meta_path("drift_check.json", data_root=tmp_path).read_text(encoding="utf-8")
    )
    assert payload["status"] == "FAIL"
    assert payload["vendor_compare_skipped"] is True
    assert any("BSM" in f or "identit" in f for f in payload["failures"])


def test_identities_broken_poisoned_gamma() -> None:
    call = _consistent_euro_row(ticker=SPXW, cp="call", poison_gamma=0.9)
    put = _consistent_euro_row(ticker=SPXW_PUT, cp="put")
    table = pa.Table.from_pylist([call, put])
    report = evaluate_drift(table, thresholds=LOOSE, dt=DT, asof_ns=ASOF_NS, r=R)
    assert report.status == "FAIL"
    assert report.beyond_by_identity["gamma_pair"] >= 1
    assert any("identit" in f for f in report.failures)


def test_null_vendor_pair_identities_hold() -> None:
    call = _consistent_euro_row(ticker=SPXW, cp="call")
    put = _consistent_euro_row(ticker=SPXW_PUT, cp="put")
    table = pa.Table.from_pylist([call, put])
    report = evaluate_drift(table, thresholds=LOOSE, dt=DT, asof_ns=ASOF_NS, r=R)
    assert report.status == "PASS"
    assert report.vendor_compare_skipped is True
    assert report.counts["atm_pairs"] == 1
    assert report.beyond_by_identity["gamma_pair"] == 0
    assert report.beyond_by_identity["vega_pair"] == 0
    assert report.beyond_by_identity["pcp"] == 0
    assert report.beyond_by_identity["reprice"] == 0


def test_missing_partition_fails(tmp_path: Path) -> None:
    rc = main([*_CLI, "--data-root", str(tmp_path)])
    assert rc == 1
    payload = json.loads(
        landing.meta_path("drift_check.json", data_root=tmp_path).read_text(encoding="utf-8")
    )
    assert payload["status"] == "FAIL"
    assert payload["failures"]


def test_aligned_cli_pass_and_poisoned_cli_nonzero(tmp_path: Path) -> None:
    _aligned_warehouse(tmp_path)
    common = [*_CLI, "--data-root", str(tmp_path)]
    assert main(common) == 0
    _aligned_warehouse(tmp_path, poison_iv=0.99)
    assert main(common) == 1


def test_skip_counts_expired_and_missing_quote(tmp_path: Path) -> None:
    last = _spxw_last()
    good = _spxw_snap(last)
    expired = _spxw_snap(last)
    expired["ticker"] = "O:SPXW260801C07700000"
    expired["details_expiration_date"] = "2026-08-01"
    no_px = _spxw_snap(last)
    no_px["ticker"] = "O:SPXW260918C07705000"
    no_px["details_strike_price"] = 7705.0
    no_px["last_trade_price"] = None
    no_px["day_close"] = None
    fwd = [forward_row(underlying="I:SPX", expiry=EXPIRY, forward=F_SPX, asof_ns=ASOF_NS)]
    _write(tmp_path, snap=[good, expired, no_px], fwd=fwd)
    row = greeks_asof(
        DT,
        ASOF_NS,
        r=R,
        data_root=tmp_path,
        roots=("SPXW",),
        crr_steps=21,
        uninvertible="skip",
    ).to_pylist()[0]
    _write(tmp_path, snap=[_vendor_matching_own(good, row), expired, no_px], fwd=fwd)
    counts = ChainCounts()
    table = greeks_asof(
        DT,
        ASOF_NS,
        r=R,
        data_root=tmp_path,
        roots=("SPXW",),
        crr_steps=21,
        uninvertible="skip",
        counts=counts,
    )
    assert counts.n_expired == 1
    assert counts.n_no_price == 1
    assert table.num_rows == 1
    report = evaluate_drift(
        table,
        counts=counts,
        thresholds=LOOSE,
        dt=DT,
        asof_ns=ASOF_NS,
        r=R,
    )
    assert report.status == "PASS"
    assert report.counts["expired"] == 1
    assert report.counts["no_price"] == 1


def test_cutoff_is_1640_et_on_the_partition_date() -> None:
    ns = cutoff_asof_ns(DT, "16:40")
    assert ns > ASOF_NS
    got = datetime.fromtimestamp(ns / 1e9, tz=ET)
    assert got.hour == 16 and got.minute == 40


def test_evaluate_does_not_trip_on_a_single_name_inside_band() -> None:
    """Far-OTM ticks are not the trigger; the rule is median / ATM fraction."""
    row = _consistent_euro_row(ticker=SPXW, cp="call")
    row.update(
        {
            "vendor_iv": 0.16,
            "vendor_delta": row["own_delta"],
            "vendor_gamma": row["own_gamma"],
            "vendor_vega": row["own_vega"] / VENDOR_VEGA_TO_PER_1,
            "vendor_theta": row["own_theta"] / VENDOR_THETA_TO_YEAR,
        }
    )
    table = pa.Table.from_pylist([row])
    report = evaluate_drift(table, thresholds=LOOSE, dt=DT, asof_ns=ASOF_NS, r=R)
    assert isinstance(report, DriftReport)
    assert report.status == "PASS"
    assert report.vendor_compare_skipped is False


def test_vendor_present_but_too_few_to_fail_is_skipped() -> None:
    """A single drifting vendor name must not FAIL when min_compare is 20."""
    row = _consistent_euro_row(ticker=SPXW, cp="call")
    row["vendor_iv"] = 0.99
    row["vendor_delta"] = 0.05
    table = pa.Table.from_pylist([row])
    report = evaluate_drift(
        table, thresholds=DEFAULT_THRESHOLDS, dt=DT, asof_ns=ASOF_NS, r=R
    )
    assert report.vendor_compare_skipped is True
    assert report.status == "FAIL"
    assert any("identities" in f for f in report.failures)
    assert not any("ΔIV" in f or "vendor ATM" in f for f in report.failures)


def test_default_thresholds_are_the_documented_canary() -> None:
    t = DEFAULT_THRESHOLDS
    assert t.iv_median_abs == 0.04
    assert t.fail_frac == 0.25
    assert t.min_compare == 20
    assert t.atm_pct == 0.05
    assert t.reprice_median_abs == 0.05


def test_oserror_writes_fail_stub_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filesystem errors must take the FAIL path (stub report + exit 1)."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("pricing.drift_check.run_drift", boom)
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
            "--force",
        ]
    )
    assert rc == 1
    payload = json.loads(
        landing.meta_path("drift_check.json", data_root=tmp_path).read_text(encoding="utf-8")
    )
    assert payload["status"] == "FAIL"
    assert any("disk full" in f for f in payload["failures"])


def test_webhook_closes_urlopen(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []

    class FakeResp:
        def __enter__(self) -> FakeResp:
            return self

        def __exit__(self, *_args: object) -> None:
            closed.append(True)

    def fake_urlopen(_req: object, timeout: float = 5) -> FakeResp:
        assert timeout == 5
        return FakeResp()

    monkeypatch.setattr("pricing.drift_check.urllib.request.urlopen", fake_urlopen)
    drift_mod._post_webhook("http://example.test/hook", {"status": "FAIL"})
    assert closed == [True]


def test_slack_webhook_wraps_text_field(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict] = []

    class FakeResp:
        def __enter__(self) -> FakeResp:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_urlopen(req: object, timeout: float = 5) -> FakeResp:
        captured.append(json.loads(req.data.decode("utf-8")))  # type: ignore[attr-defined]
        return FakeResp()

    monkeypatch.setattr("pricing.drift_check.urllib.request.urlopen", fake_urlopen)
    drift_mod._post_webhook(
        "https://hooks.slack.com/services/T/B/X",
        {"status": "FAIL", "date": DT.isoformat(), "failures": ["median |ΔIV| too large"]},
    )
    assert len(captured) == 1
    assert "text" in captured[0]
    assert "FAIL drift_check" in captured[0]["text"]
    assert "median |ΔIV|" in captured[0]["text"]

    captured.clear()
    drift_mod._post_webhook("http://example.test/hook", {"status": "FAIL", "date": DT.isoformat()})
    assert captured[0]["status"] == "FAIL"
    assert "text" not in captured[0]
