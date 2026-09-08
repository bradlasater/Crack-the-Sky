"""Daily identity canary: own math first; vendor diffs diagnostic when present."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pytest

from ingest.common import landing
from pricing import drift_check as drift_mod
from pricing import surface as sf
from pricing.bsm import price as bsm_price
from pricing.bsm import raw_greeks
from pricing.conventions import CALENDAR_DAYS_PER_YEAR
from pricing.daycount import ACT_365
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
from pricing.from_market import CHAIN_CRR_STEPS, ChainCounts, greeks_asof
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
from tests.pricing.test_surface import (
    FAR,
    MID,
    NEAR,
    T_FAR,
    T_MID,
    T_NEAR,
    _flat_rate,
    _svi_bars,
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
    poison_t: float | None = None,
    poison_market: float | None = None,
) -> dict:
    S, K, T, rate, q, sig = 7700.0, 7700.0, 0.05, 0.04, 0.0, 0.16
    g = raw_greeks(S, K, T, rate, sig, cp, q=q)
    mkt = float(bsm_price(S, K, T, rate, sig, cp, q=q))
    row_t = poison_t if poison_t is not None else T
    return {
        "ticker": ticker,
        "root": "SPXW",
        "expiry": EXPIRY,
        "call_put": cp,
        "strike": K,
        "F": K,
        "S": S,
        "T": row_t,
        "r": rate,
        "q": q,
        "exercise_style": "european",
        "greeks_engine": "european_bsm",
        "own_iv": sig,
        "own_price": mkt,
        "own_delta": float(g["delta"]),
        "own_gamma": float(g["gamma"]),
        "own_vega": float(g["vega"]),
        "own_theta": float(g["theta"]),
        "market_price": poison_market if poison_market is not None else mkt,
        "price_source": "close",
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
    assert any("own_price" in f or "identit" in f for f in payload["failures"])


def test_identities_broken_poisoned_gamma() -> None:
    # Poison T for the call to create a genuine BSM-gamma difference vs the put.
    # At T≈0 gamma spikes while the put's T=0.05 gamma is much lower; the pair
    # check must catch the discrepancy now that it recomputes at shared sigma.
    call = _consistent_euro_row(ticker=SPXW, cp="call", poison_t=0.001)
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


def test_cent_reprice_residual_fails_the_solver_band() -> None:
    """A $0.01 invert/reprice break sat inside the old $0.05 nickel (issue #43)."""
    row = _consistent_euro_row(ticker=SPXW, cp="call")
    row["market_price"] = float(row["own_price"]) + 0.01
    report = evaluate_drift(
        pa.Table.from_pylist([row]), thresholds=LOOSE, dt=DT, asof_ns=ASOF_NS, r=R
    )
    assert report.status == "FAIL"
    assert report.beyond_by_identity["reprice"] == 1
    assert report.median_abs_reprice == pytest.approx(0.01)
    assert any("own_price" in f for f in report.failures)


def test_american_scale_reprice_residual_stays_inside_the_solver_band() -> None:
    """Measured American p90 at 51 CRR steps is ~2e-6; that must not FAIL."""
    row = _consistent_euro_row(ticker=SPXW, cp="call")
    row["market_price"] = float(row["own_price"]) + 2e-6
    report = evaluate_drift(
        pa.Table.from_pylist([row]), thresholds=LOOSE, dt=DT, asof_ns=ASOF_NS, r=R
    )
    assert report.status == "PASS"
    assert report.beyond_by_identity["reprice"] == 0
    assert report.median_abs_reprice == pytest.approx(2e-6)


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
    assert t.iv_abs == 0.04
    assert t.iv_rel == 0.25
    assert t.delta_abs == 0.08
    assert t.delta_rel == 0.30
    assert t.gamma_abs == 0.005
    assert t.gamma_rel == 0.75
    assert t.vega_abs == 25.0
    assert t.vega_rel == 0.50
    assert t.theta_abs == 150.0
    assert t.theta_rel == 0.60
    assert t.iv_median_abs == 0.04
    assert t.reprice_abs == 1e-3
    assert t.reprice_rel == 0.0
    assert t.reprice_median_abs == 1e-4
    # Bands were measured at this tree depth (issue #43); a step change
    # needs a new measurement, not a silent reuse of 1e-3 / 1e-4.
    assert CHAIN_CRR_STEPS == 51
    assert t.gamma_pair_abs == 0.002
    assert t.gamma_pair_rel == 0.35
    assert t.vega_pair_abs == 10.0
    assert t.vega_pair_rel == 0.35
    assert t.pcp_abs == 1.0
    assert t.pcp_rel == 0.05
    assert t.fail_frac == 0.25
    assert t.min_compare == 20
    assert t.atm_pct == 0.05
    assert drift_mod.DEFAULT_FAIL_FRAC == 0.25
    assert drift_mod.DEFAULT_MIN_COMPARE == 20
    assert drift_mod.DEFAULT_ATM_PCT == 0.05
    assert drift_mod.DEFAULT_SPY_ATM_PCT == 0.05
    assert drift_mod.DEFAULT_MAX_ROWS == 400
    assert drift_mod.DEFAULT_CUTOFF_ET == "16:40"
    assert drift_mod.DEFAULT_R == 0.04


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


# ---------------------------------------------------------------------------
# Terminal-event guarantee
# ---------------------------------------------------------------------------
#
# On 2026-09-01 the 17:00 cron run of this job logged job_start and then
# nothing: no job_end, no job_error, no Healthchecks ping. The exception
# taxonomy main() catches is narrow, and anything outside it left the run
# silent -- the pricing canary failing in exactly the way it exists to detect.

def _drift_pings(monkeypatch) -> list[tuple[str, bytes]]:
    calls: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        drift_mod, "ping",
        lambda url, suffix="", autocreate=False, body=None: calls.append(
            ((url or "") + suffix, body or b"")
        ),
    )
    return calls


@pytest.mark.parametrize("exc", [MemoryError("oom"), RuntimeError("boom"), KeyError("k")])
def test_unhandled_exception_still_reports_before_propagating(
    tmp_path, monkeypatch, exc
) -> None:
    """An exception outside the taxonomy must ping /fail, then re-raise."""
    calls = _drift_pings(monkeypatch)
    events: list[tuple[str, dict]] = []

    class _Logger:
        def log(self, event, **fields):
            events.append((event, fields))

        def close(self):
            pass

    monkeypatch.setattr(drift_mod, "get_run_logger", lambda *a, **k: _Logger())
    monkeypatch.setattr(drift_mod, "require_trading_day", lambda *a, **k: None)

    def _explode(*a, **k):
        raise exc

    monkeypatch.setattr(drift_mod, "run_drift", _explode)

    with pytest.raises(type(exc)):
        drift_mod.main(["--date", "2026-09-01", "--data-root", str(tmp_path)])

    assert any(e == "job_error" and f.get("unhandled") for e, f in events), events
    assert any(url.endswith("/fail") for url, _ in calls), calls


def test_successful_run_logs_exactly_one_terminal_event(tmp_path, monkeypatch) -> None:
    """The healthy path must be accounted for too, or the guarantee is empty."""
    calls = _drift_pings(monkeypatch)
    events: list[tuple[str, dict]] = []

    class _Logger:
        def log(self, event, **fields):
            events.append((event, fields))

        def close(self):
            pass

    monkeypatch.setattr(drift_mod, "get_run_logger", lambda *a, **k: _Logger())
    monkeypatch.setattr(drift_mod, "require_trading_day", lambda *a, **k: None)

    report = drift_mod.DriftReport(
        date="2026-09-01", asof_ns=1, cutoff_et="16:40", r=None,
        status=drift_mod.PASS, failures=[], counts={"priced": 1},
    )
    monkeypatch.setattr(drift_mod, "run_drift", lambda *a, **k: report)

    rc = drift_mod.main(
        ["--date", "2026-09-01", "--data-root", str(tmp_path), "--dry-run"]
    )
    assert rc == 0
    terminal = [e for e, _ in events if e in ("job_end", "job_error")]
    assert terminal == ["job_end"], events
    assert sum(1 for url, _ in calls if not url.endswith("/start")) == 1, calls


def test_invalid_config_still_reports_a_terminal_event(tmp_path, monkeypatch) -> None:
    """Validation failures happen inside the accounted run, not before it.

    A bad DRIFT_CHECK_R used to return 1 before the logger or the /start ping
    existed, so a run that failed instantly looked exactly like a run that was
    never scheduled.
    """
    calls = _drift_pings(monkeypatch)
    events: list[tuple[str, dict]] = []

    class _Logger:
        def log(self, event, **fields):
            events.append((event, fields))

        def close(self):
            pass

    monkeypatch.setattr(drift_mod, "get_run_logger", lambda *a, **k: _Logger())
    monkeypatch.setenv("DRIFT_CHECK_R", "not-a-number")

    rc = drift_mod.main(["--date", "2026-09-01", "--data-root", str(tmp_path)])
    assert rc == 1
    assert any(e == "job_error" for e, _ in events), events
    assert any(url.endswith("/fail") for url, _ in calls), calls


def test_dotenv_is_patchable_so_tests_cannot_reach_production(tmp_path) -> None:
    """The credential scrub in tests/conftest.py must actually cover this job.

    ``_dotenv`` resolves the parser through the config *module*; a
    ``from ... import _parse_env_file`` alias would be bound before any
    fixture runs, and this job pings on every run.
    """
    assert "HEALTHCHECKS_PING_KEY" not in drift_mod._dotenv()
    url, _ = drift_mod._hc_target(drift_mod._dotenv())
    assert url is None


# ---------------------------------------------------------------------------
# Staleness reporting (issue #44, option 2 folded in)
# ---------------------------------------------------------------------------

def test_canary_report_carries_staleness_counts(tmp_path: Path) -> None:
    """Stale-skipped and below-intrinsic rows are explicit report fields."""
    last = _spxw_last()
    good = _null_vendor(_spxw_snap(last, vendor_iv=None, vendor_delta=None))
    stale = _spxw_snap(last)
    stale["ticker"] = "O:SPXW260918C07705000"
    stale["details_strike_price"] = 7705.0
    stale["last_trade_sip_timestamp_ns"] = ASOF_NS - 3 * 3_600_000_000_000  # 3 h old
    below = _spxw_snap(last)
    below["ticker"] = "O:SPXW260918P07705000"
    below["details_contract_type"] = "put"
    below["details_strike_price"] = 7705.0
    # European put floor is (K - F) e^{-rT} ≈ 4.99 here; 4.50 is below it.
    below["last_trade_price"] = 4.50
    below["day_close"] = 4.50
    below["last_trade_sip_timestamp_ns"] = ASOF_NS
    fwd = [forward_row(underlying="I:SPX", expiry=EXPIRY, forward=F_SPX, asof_ns=ASOF_NS)]
    _write(tmp_path, snap=[good, stale, below], fwd=fwd)
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
    assert report.status == "PASS"
    assert report.counts["stale"] == 1
    assert report.counts["uninvertible"] == 1
    assert report.counts["below_intrinsic"] == 1
    assert report.counts["priced"] == 1
    assert report.max_trade_age_min == 60.0
    rendered = drift_mod._render(report)
    assert "below_intrinsic=1" in rendered
    assert "stale=1" in rendered


def test_cli_max_trade_age_flag(tmp_path: Path) -> None:
    _null_vendor_warehouse(tmp_path)
    rc = main([*_CLI, "--data-root", str(tmp_path), "--max-trade-age-min", "30"])
    assert rc == 0
    payload = json.loads(
        landing.meta_path("drift_check.json", data_root=tmp_path).read_text(encoding="utf-8")
    )
    assert payload["status"] == "PASS"
    assert payload["max_trade_age_min"] == 30.0
    assert payload["counts"]["stale"] == 0


# ---------------------------------------------------------------------------
# Off-ATM surface consumer
# ---------------------------------------------------------------------------
#
# The canary reads the landed vol_surface SVI params back and round-trips a
# fixed log-moneyness grid through the engine (price off the slice vol in the
# forward measure, invert back with pricing.iv.implied_vol). Only a missing
# partition directory is a skip; data that exists but cannot be reproduced is
# a FAIL. The checked session is the previous trading day: the scheduled
# surface build lands session S's smile the next morning at 12:15, before the
# 17:00 canary for S+1.

SURFACE_PREV = date(2026, 8, 27)  # previous trading day before DT (Fri 2026-08-28)


def _land_surface(tmp_path: Path, poison_a: float | None = None, d: date = DT) -> None:
    """Fit the synthetic three-expiry smile and land it under tmp_path."""
    from ingest.common.config import Settings

    bars = (_svi_bars(NEAR, T_NEAR) + _svi_bars(MID, T_MID) + _svi_bars(FAR, T_FAR))
    surfaces = sf.build_surfaces(bars, d, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)
    rows = sf.rows_from_surfaces(surfaces)
    if poison_a is not None:
        rows = [dict(r, svi_a=poison_a) for r in rows]
    settings = Settings(massive_api_key="k", data_root=tmp_path, log_root=tmp_path / "logs")
    sf.write_rows(settings, d, rows)


def test_canary_checks_the_previous_trading_days_surface(tmp_path: Path) -> None:
    """The 12:15 surface build fits T-1, so the 17:00 canary must look at T-1."""
    from ingest.common.market_gate import previous_trading_day

    assert previous_trading_day(DT, tmp_path) == SURFACE_PREV
    _null_vendor_warehouse(tmp_path)
    _land_surface(tmp_path, d=SURFACE_PREV)
    report = run_drift(
        DT, r=R, data_root=tmp_path, asof_ns=ASOF_NS, roots=("SPXW",),
        crr_steps=21, max_rows=50, uninvertible="skip", thresholds=LOOSE,
    )
    assert report.status == "PASS"
    assert report.surface_check is not None
    assert report.surface_check["date"] == SURFACE_PREV.isoformat()
    assert report.surface_check["skipped"] is False
    assert report.surface_check["points"] == 4
    assert f"surface {SURFACE_PREV.isoformat()} max_|Δσ|" in drift_mod._render(report)


def test_canary_date_own_surface_partition_is_not_read(tmp_path: Path) -> None:
    """A partition for the canary date itself cannot exist yet on the schedule."""
    _null_vendor_warehouse(tmp_path)
    _land_surface(tmp_path, d=DT)
    report = run_drift(
        DT, r=R, data_root=tmp_path, asof_ns=ASOF_NS, roots=("SPXW",),
        crr_steps=21, max_rows=50, uninvertible="skip", thresholds=LOOSE,
    )
    assert report.status == "PASS"
    assert report.surface_check["skipped"] is True
    assert report.surface_check["date"] == SURFACE_PREV.isoformat()


def test_offatm_surface_round_trips_through_the_engine(tmp_path: Path) -> None:
    _land_surface(tmp_path)
    result = drift_mod.check_offatm_surface(DT, data_root=tmp_path)
    assert result.skipped is False
    assert result.failures == []
    # The DTE targets pick the 28d and 112d slices; the ±0.10 grid points lie
    # outside the fixture's quoted strikes, so each slice contributes ±0.05.
    assert result.roots == ["SPXW"]
    assert result.slices == 2
    assert result.points == 4
    assert result.max_abs_vol is not None
    assert result.max_abs_vol < DEFAULT_THRESHOLDS.surface_vol_abs


def test_offatm_surface_missing_partition_skips(tmp_path: Path) -> None:
    result = drift_mod.check_offatm_surface(DT, data_root=tmp_path)
    assert result.skipped is True
    assert result.failures == []
    assert result.points == 0


def test_offatm_surface_empty_partition_dir_fails(tmp_path: Path) -> None:
    """An existing-but-empty dt= directory is an interrupted write, not a skip."""
    (tmp_path / "clean" / "vol_surface" / f"dt={DT.isoformat()}").mkdir(parents=True)
    result = drift_mod.check_offatm_surface(DT, data_root=tmp_path)
    assert result.skipped is False
    assert any("no parquet" in f for f in result.failures)


def test_offatm_surface_partition_without_surface_roots_fails(tmp_path: Path) -> None:
    from ingest.common.config import Settings

    settings = Settings(massive_api_key="k", data_root=tmp_path, log_root=tmp_path / "logs")
    sf.write_rows(settings, DT, [
        dict(r, underlying="SPY")
        for r in sf.rows_from_surfaces(
            sf.build_surfaces(_svi_bars(NEAR, T_NEAR), DT, roots=("SPXW",),
                              rate_fn=_flat_rate, daycount=ACT_365))
    ])
    result = drift_mod.check_offatm_surface(DT, data_root=tmp_path)
    assert result.skipped is False
    assert any("no" in f and "rows" in f for f in result.failures)


def test_offatm_surface_misplaced_partition_rows_fail(tmp_path: Path) -> None:
    """Rows dated for another session under this dt= must not answer as T."""
    from ingest.common.config import Settings

    bars = _svi_bars(NEAR, T_NEAR)
    surfaces = sf.build_surfaces(bars, DT, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)
    settings = Settings(massive_api_key="k", data_root=tmp_path, log_root=tmp_path / "logs")
    sf.write_rows(settings, DT, [dict(r, date="2020-01-02")
                                 for r in sf.rows_from_surfaces(surfaces)])
    result = drift_mod.check_offatm_surface(DT, data_root=tmp_path)
    assert result.skipped is False
    assert any("have date=" in f for f in result.failures)
    assert result.points == 0


def test_offatm_surface_malformed_rows_fail_without_raising(tmp_path: Path) -> None:
    """A landed file missing a schema column is corrupt data, not a crash."""
    import pyarrow.parquet as pq

    bars = _svi_bars(NEAR, T_NEAR)
    surfaces = sf.build_surfaces(bars, DT, roots=("SPXW",), rate_fn=_flat_rate, daycount=ACT_365)
    rows = [{k: v for k, v in r.items() if k != "svi_rho"}
            for r in sf.rows_from_surfaces(surfaces)]
    part = tmp_path / "clean" / "vol_surface" / f"dt={DT.isoformat()}"
    part.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows), part / "part-0.parquet")
    result = drift_mod.check_offatm_surface(DT, data_root=tmp_path)
    assert result.skipped is False
    assert any("KeyError" in f and "svi_rho" in f for f in result.failures)


def test_offatm_surface_poisoned_params_fail_loud(tmp_path: Path) -> None:
    """Negative total variance at the grid is landed data the engine cannot price."""
    _land_surface(tmp_path, poison_a=-0.01)
    result = drift_mod.check_offatm_surface(DT, data_root=tmp_path)
    assert result.skipped is False
    assert result.points == 0
    assert result.failures
    assert all("not evaluable" in f for f in result.failures)


def test_offatm_surface_fail_trips_the_canary(tmp_path: Path) -> None:
    _null_vendor_warehouse(tmp_path)
    _land_surface(tmp_path, poison_a=-0.01, d=SURFACE_PREV)
    rc = main([*_CLI, "--data-root", str(tmp_path)])
    assert rc == 1
    payload = json.loads(
        landing.meta_path("drift_check.json", data_root=tmp_path).read_text(encoding="utf-8")
    )
    assert payload["status"] == "FAIL"
    assert any(f.startswith("surface:") for f in payload["failures"])
    assert payload["surface_check"]["date"] == SURFACE_PREV.isoformat()
    assert payload["surface_check"]["failures"]


def test_offatm_surface_skip_still_passes_and_logs(tmp_path: Path, monkeypatch) -> None:
    """No surface build on the box: skip, not FAIL, and say so in the run log."""
    _null_vendor_warehouse(tmp_path)
    events: list[tuple[str, dict]] = []

    class _Logger:
        def log(self, event, **fields):
            events.append((event, fields))

        def close(self):
            pass

    monkeypatch.setattr(drift_mod, "get_run_logger", lambda *a, **k: _Logger())
    rc = main([*_CLI, "--data-root", str(tmp_path)])
    assert rc == 0
    skip_events = [f for e, f in events if e == "surface_compare_skipped"]
    assert skip_events and skip_events[0]["surface_date"] == SURFACE_PREV.isoformat(), events
    payload = json.loads(
        landing.meta_path("drift_check.json", data_root=tmp_path).read_text(encoding="utf-8")
    )
    assert payload["status"] == "PASS"
    assert payload["surface_check"]["skipped"] is True


def test_default_thresholds_pin_the_surface_band() -> None:
    assert DEFAULT_THRESHOLDS.surface_vol_abs == 1e-4
    assert drift_mod.SURFACE_K_GRID == (-0.10, -0.05, 0.05, 0.10)
    assert drift_mod.SURFACE_TARGET_DTE == (30, 90)


# ---------------------------------------------------------------------------
# --date validation (monitoring gap: argparse exit 2, not a bare traceback)
# ---------------------------------------------------------------------------

def test_malformed_date_exits_2_before_any_run(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--date", "not-a-date", "--data-root", str(tmp_path)])
    assert excinfo.value.code == 2
    # No report stub, no half-run: argparse rejected it before the job started.
    assert not landing.meta_path("drift_check.json", data_root=tmp_path).exists()


def test_valid_date_still_parses(tmp_path: Path) -> None:
    _null_vendor_warehouse(tmp_path)
    rc = main([*_CLI, "--data-root", str(tmp_path)])
    assert rc == 0
