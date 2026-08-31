"""Tests for coverage_audit: the job that makes missing data loud.

A first run that passes everything means the audit is not actually checking,
so these assert both directions explicitly.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ingest.common import landing
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import coverage_audit as audit

RUN_DATE = date(2026, 8, 28)  # a Friday


def _settings(data_root: Path) -> Settings:
    return Settings(
        massive_api_key="test-key",
        data_root=data_root,
        log_root=data_root / "logs",
    )


def _logger() -> JsonlLogger:
    return JsonlLogger(path=None, echo=False)


def _trade(ticker: str) -> dict:
    return {
        "ticker": ticker, "price": 1.0, "size": 1, "exchange": 300,
        "conditions": "[]", "correction": 0, "trade_id": "x",
        "sequence_number": 1, "sip_timestamp_ns": 1, "src": "flatfile",
    }


def _write_manifest(root: Path, day: date, datasets=("trades_v1", "minute_aggs_v1", "day_aggs_v1")) -> None:
    path = landing.meta_path("flatfile_manifest.json", data_root=root)
    path.write_text(json.dumps([
        {"dataset": ds, "date": day.isoformat(), "bytes": 1,
         "rows_in": 100, "rows_kept": 10, "md5": "x"}
        for ds in datasets
    ]), encoding="utf-8")


# ---------------------------------------------------------------------------
# Expected sweep count
# ---------------------------------------------------------------------------

def test_expected_sweeps_regular_session(tmp_path: Path) -> None:
    # 09:30 -> 16:20 (close + 20 min buffer) = 410 minutes.
    assert audit.expected_sweeps(RUN_DATE, tmp_path) == 410


def test_expected_sweeps_shrinks_on_early_close(tmp_path: Path) -> None:
    """Early closes must not be flagged as missing data."""
    early = date(2026, 11, 27)
    meta = tmp_path / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "holidays.json").write_text(json.dumps([
        {"date": early.isoformat(), "exchange": "NYSE",
         "name": "Thanksgiving", "status": "early-close"}
    ]), encoding="utf-8")
    from ingest.common import market_gate
    market_gate._holiday_cache.clear()
    assert audit.expected_sweeps(early, tmp_path) < audit.expected_sweeps(RUN_DATE, tmp_path)
    market_gate._holiday_cache.clear()


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def test_snapshots_fail_when_nothing_landed(tmp_path: Path) -> None:
    checks = audit.check_snapshots(_settings(tmp_path), RUN_DATE)
    assert {c.status for c in checks} == {audit.FAIL}
    assert all("no sweeps landed" in c.detail for c in checks)


def test_snapshots_pass_at_full_cadence(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    part = tmp_path / "clean" / "option_snapshots" / f"dt={RUN_DATE.isoformat()}"
    part.mkdir(parents=True)
    expected = audit.expected_sweeps(RUN_DATE, tmp_path)
    base = 1788000000000
    for root in ("SPY", "I:SPX"):
        for i in range(expected):
            (part / f"snapshot_sweep-{root}-{base + i * 60_000}.parquet").touch()
    checks = audit.check_snapshots(settings, RUN_DATE)
    assert {c.status for c in checks} == {audit.PASS}


def test_snapshots_warn_on_a_hole(tmp_path: Path) -> None:
    """Full count but a 10-minute hole is still a gap."""
    settings = _settings(tmp_path)
    part = tmp_path / "clean" / "option_snapshots" / f"dt={RUN_DATE.isoformat()}"
    part.mkdir(parents=True)
    expected = audit.expected_sweeps(RUN_DATE, tmp_path)
    base = 1788000000000
    for root in ("SPY", "I:SPX"):
        for i in range(expected):
            offset = i * 60_000 + (600_000 if i > expected // 2 else 0)
            (part / f"snapshot_sweep-{root}-{base + offset}.parquet").touch()
    statuses = {c.status for c in audit.check_snapshots(settings, RUN_DATE)}
    assert audit.WARN in statuses or audit.FAIL in statuses


def test_flatfiles_fail_when_absent_and_pass_when_complete(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert {c.status for c in audit.check_flatfiles(settings, RUN_DATE)} == {audit.FAIL}
    _write_manifest(tmp_path, RUN_DATE)
    assert {c.status for c in audit.check_flatfiles(settings, RUN_DATE)} == {audit.PASS}


def test_flatfile_pulled_but_empty_is_a_failure(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = landing.meta_path("flatfile_manifest.json", data_root=tmp_path)
    path.write_text(json.dumps([
        {"dataset": ds, "date": RUN_DATE.isoformat(), "rows_in": 100, "rows_kept": 0}
        for ds in ("trades_v1", "minute_aggs_v1", "day_aggs_v1")
    ]), encoding="utf-8")
    checks = audit.check_flatfiles(settings, RUN_DATE)
    assert {c.status for c in checks} == {audit.FAIL}
    assert all("kept 0 rows" in c.detail for c in checks)


def test_websocket_fails_when_it_never_ran(tmp_path: Path) -> None:
    """The state this repo was actually in: WS scheduled but never producing."""
    checks = audit.check_websocket(_settings(tmp_path), RUN_DATE, _logger())
    assert checks[0].status == audit.FAIL
    assert "did not run" in checks[0].detail


def test_websocket_passes_with_capture_files(tmp_path: Path) -> None:
    raw = tmp_path / "raw" / "option_minute_bars_ws" / f"dt={RUN_DATE.isoformat()}"
    raw.mkdir(parents=True)
    (raw / "ws_minute_bars-1.jsonl").write_text('{"sym":"O:SPY"}\n', encoding="utf-8")
    checks = audit.check_websocket(_settings(tmp_path), RUN_DATE, _logger())
    assert checks[0].status == audit.PASS


def test_underlying_coverage_flags_a_missing_root(tmp_path: Path) -> None:
    """The SPX-shaped hole: healthy row count, one side empty."""
    settings = _settings(tmp_path)
    landing.write_clean(
        "option_trades", RUN_DATE,
        [_trade("O:SPY260918C00770000") for _ in range(1000)],
        job="flatfile_pull", data_root=tmp_path,
    )
    checks = audit.check_underlying_coverage(settings, RUN_DATE)
    failed = {c.name for c in checks if c.status == audit.FAIL}
    assert "underlying[SPX]" in failed
    assert "underlying[SPXW]" in failed


def test_underlying_coverage_flags_foreign_roots(tmp_path: Path) -> None:
    """Leveraged ETFs must not be admitted as SPY/SPX options."""
    settings = _settings(tmp_path)
    records = [_trade(t) for t in (
        "O:SPY260918C00770000", "O:SPX260918C08000000",
        "O:SPXW260918P07600000", "O:SPXL260918C00250000",
    )]
    landing.write_clean("option_trades", RUN_DATE, records,
                        job="flatfile_pull", data_root=tmp_path)
    checks = {c.name: c for c in audit.check_underlying_coverage(settings, RUN_DATE)}
    assert checks["ticker_purity"].status == audit.FAIL
    assert "SPXL" in checks["ticker_purity"].detail


def test_underlying_coverage_passes_when_clean(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    records = [_trade(t) for t in (
        "O:SPY260918C00770000", "O:SPX260918C08000000", "O:SPXW260918P07600000",
    )]
    landing.write_clean("option_trades", RUN_DATE, records,
                        job="flatfile_pull", data_root=tmp_path)
    checks = audit.check_underlying_coverage(settings, RUN_DATE)
    assert all(c.status == audit.PASS for c in checks)


def test_render_lists_every_check(tmp_path: Path) -> None:
    checks = [audit.Check("a", audit.PASS, "ok"), audit.Check("b", audit.FAIL, "bad")]
    out = audit._render(RUN_DATE, checks)
    assert "PASS" in out and "FAIL" in out and RUN_DATE.isoformat() in out
