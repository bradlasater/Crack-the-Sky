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
    # 09:30 -> 16:30 (close + the crontab's 30-minute tail), both endpoints
    # firing, = 421 sweeps. Not close + 20: that is the websocket deadline,
    # and borrowing it here understated the day by ten sweeps.
    assert audit.expected_sweeps(RUN_DATE, tmp_path) == 421


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

def _window_base_ms(tmp_path: Path) -> int:
    """Epoch-ms of the first scheduled in-session sweep on RUN_DATE."""
    open_et, _ = audit.sweep_window(RUN_DATE, tmp_path)
    return int(open_et.timestamp() * 1000)


def _singleton_ms(tmp_path: Path, which: str) -> int:
    """Epoch-ms of the pre-open or EOD sweep, as the crontab schedules it."""
    from datetime import datetime

    from ingest.common import market_gate
    at = audit.PREOPEN_SWEEP_ET if which == "preopen" else audit.EOD_SWEEP_ET
    return int(datetime.combine(RUN_DATE, at, tzinfo=market_gate.ET).timestamp() * 1000)


def _land_full_day(tmp_path: Path, roots=("SPY", "I:SPX", "VIX")) -> Path:
    """A healthy day exactly as the crontab produces it, singletons included."""
    part = tmp_path / "clean" / "option_snapshots" / f"dt={RUN_DATE.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    expected = audit.expected_sweeps(RUN_DATE, tmp_path)
    base = _window_base_ms(tmp_path)
    for root in roots:
        for i in range(expected):
            (part / f"snapshot_sweep-{root}-{base + i * 60_000}.parquet").touch()
        for which in ("preopen", "eod"):
            ms = _singleton_ms(tmp_path, which)
            (part / f"snapshot_sweep-eod-{root}-{ms}.parquet").touch()
    return part


def test_snapshots_fail_when_nothing_landed(tmp_path: Path) -> None:
    checks = audit.check_snapshots(_settings(tmp_path), RUN_DATE)
    assert {c.status for c in checks} == {audit.FAIL}
    per_root = [c for c in checks if c.name.startswith("snapshots[")]
    assert all("no in-session sweeps landed" in c.detail for c in per_root)


def test_snapshots_pass_at_full_cadence(tmp_path: Path) -> None:
    _land_full_day(tmp_path)
    checks = audit.check_snapshots(_settings(tmp_path), RUN_DATE)
    assert {c.status for c in checks} == {audit.PASS}


def test_scheduled_preopen_and_eod_sweeps_are_not_gaps(tmp_path: Path) -> None:
    """The regression this check existed to have: a healthy day must be clean.

    The 09:05 -> 09:30 wait and the 16:30 -> 16:35 wait are both far beyond
    MAX_SWEEP_GAP_S. Scanning the whole partition made every single day WARN
    on SPY and SPX, which is how the one dataset that cannot be backfilled
    ended up with an alarm nobody could act on.
    """
    _land_full_day(tmp_path)
    checks = audit.check_snapshots(_settings(tmp_path), RUN_DATE)
    per_root = {c.name: c for c in checks if c.name.startswith("snapshots[")}
    assert {c.status for c in per_root.values()} == {audit.PASS}
    for check in per_root.values():
        assert check.data["max_gap_s"] <= audit.MAX_SWEEP_GAP_S
        assert check.data["preopen"] == 1
        assert check.data["eod"] == 1


def test_missing_preopen_sweep_fails_even_at_full_cadence(tmp_path: Path) -> None:
    """Settled open interest exists only in the 09:05 sweep."""
    part = _land_full_day(tmp_path)
    ms = _singleton_ms(tmp_path, "preopen")
    (part / f"snapshot_sweep-eod-VIX-{ms}.parquet").unlink()
    checks = {c.name: c for c in audit.check_snapshots(_settings(tmp_path), RUN_DATE)}
    assert checks["snapshots_preopen"].status == audit.FAIL
    assert checks["snapshots_preopen"].data["missing"] == ["VIX"]
    assert checks["snapshots[VIX]"].status == audit.PASS  # cadence itself is fine


def test_missing_eod_sweep_fails_even_at_full_cadence(tmp_path: Path) -> None:
    """drift_check reprices the EOD chain; losing it is silent otherwise."""
    part = _land_full_day(tmp_path)
    ms = _singleton_ms(tmp_path, "eod")
    (part / f"snapshot_sweep-eod-SPY-{ms}.parquet").unlink()
    checks = {c.name: c for c in audit.check_snapshots(_settings(tmp_path), RUN_DATE)}
    assert checks["snapshots_eod"].status == audit.FAIL
    assert checks["snapshots_eod"].data["missing"] == ["SPY"]


def test_out_of_session_sweeps_are_reported_but_never_counted(tmp_path: Path) -> None:
    """A sweep run by hand overnight must not pad the ratio or fake a gap.

    Manual runs land in the same dt= partition. Counting them let extra
    out-of-hours sweeps satisfy the 95% ratio while real in-session minutes
    were missing, and made the overnight wait look like an 8-hour outage.
    """
    part = _land_full_day(tmp_path)
    base = _window_base_ms(tmp_path)
    stray = base - 8 * 3600 * 1000  # ~01:30 ET, as a manual run would land
    for root in ("SPY", "I:SPX", "VIX"):
        (part / f"snapshot_sweep-{root}-{stray}.parquet").touch()
    checks = {c.name: c for c in audit.check_snapshots(_settings(tmp_path), RUN_DATE)}
    spy = checks["snapshots[SPY]"]
    assert spy.status == audit.PASS
    assert spy.data["stray"] == 1
    assert spy.data["sweeps"] == audit.expected_sweeps(RUN_DATE, tmp_path)
    assert spy.data["max_gap_s"] <= audit.MAX_SWEEP_GAP_S


def test_a_hole_is_not_masked_by_extra_out_of_session_sweeps(tmp_path: Path) -> None:
    """Strays must not buy back a genuinely missing chunk of the session."""
    part = tmp_path / "clean" / "option_snapshots" / f"dt={RUN_DATE.isoformat()}"
    part.mkdir(parents=True)
    expected = audit.expected_sweeps(RUN_DATE, tmp_path)
    base = _window_base_ms(tmp_path)
    kept = int(expected * 0.90)
    for root in ("SPY", "I:SPX", "VIX"):
        for i in range(kept):
            (part / f"snapshot_sweep-{root}-{base + i * 60_000}.parquet").touch()
        # Plenty of overnight runs -- enough to restore the raw file count.
        for j in range(expected - kept):
            ms = base - (j + 1) * 60_000 - 8 * 3600 * 1000
            (part / f"snapshot_sweep-{root}-{ms}.parquet").touch()
    checks = {c.name: c for c in audit.check_snapshots(_settings(tmp_path), RUN_DATE)}
    assert checks["snapshots[SPY]"].status == audit.FAIL
    assert "only 90% of expected sweeps" in checks["snapshots[SPY]"].detail


def test_snapshots_warn_on_a_hole(tmp_path: Path) -> None:
    """Full count but a 10-minute hole is still a gap."""
    settings = _settings(tmp_path)
    part = tmp_path / "clean" / "option_snapshots" / f"dt={RUN_DATE.isoformat()}"
    part.mkdir(parents=True)
    expected = audit.expected_sweeps(RUN_DATE, tmp_path)
    base = _window_base_ms(tmp_path)
    for root in ("SPY", "I:SPX", "VIX"):
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
        "O:VIX260916C00020000", "O:VIXW260902P00016000",
    )]
    landing.write_clean("option_trades", RUN_DATE, records,
                        job="flatfile_pull", data_root=tmp_path)
    checks = audit.check_underlying_coverage(settings, RUN_DATE)
    assert all(c.status == audit.PASS for c in checks)


def test_render_lists_every_check(tmp_path: Path) -> None:
    checks = [audit.Check("a", audit.PASS, "ok"), audit.Check("b", audit.FAIL, "bad")]
    out = audit._render(RUN_DATE, checks)
    assert "PASS" in out and "FAIL" in out and RUN_DATE.isoformat() in out


# ---------------------------------------------------------------------------
# Disk runway
# ---------------------------------------------------------------------------
#
# option_snapshots cannot be backfilled and prune_raw.sh rightly refuses to
# touch it, so a full volume is a capture outage with a long fuse. Monitor the
# fuse.

def _land_snapshot_bytes(tmp_path: Path, day: date, total: int) -> None:
    part = tmp_path / "clean" / "option_snapshots" / f"dt={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    (part / f"snapshot_sweep-SPY-{day.toordinal()}.parquet").write_bytes(b"x" * total)


def _fake_usage(monkeypatch, free: int, total: int = 1_000_000_000_000) -> None:
    import shutil as _shutil
    monkeypatch.setattr(
        audit.shutil, "disk_usage",
        lambda p: _shutil._ntuple_diskusage(total, total - free, free),
    )


def test_disk_runway_skips_without_a_growth_sample(tmp_path: Path, monkeypatch) -> None:
    _fake_usage(monkeypatch, free=500_000_000_000)
    checks = audit.check_disk(_settings(tmp_path), RUN_DATE)
    assert checks[0].status == audit.SKIP


def test_disk_runway_passes_with_headroom(tmp_path: Path, monkeypatch) -> None:
    _land_snapshot_bytes(tmp_path, RUN_DATE, 1_000_000)
    _fake_usage(monkeypatch, free=1_000_000 * 500)
    check = audit.check_disk(_settings(tmp_path), RUN_DATE)[0]
    assert check.status == audit.PASS
    assert check.data["days_remaining"] == 500.0


def test_disk_runway_warns_then_fails_as_it_shrinks(tmp_path: Path, monkeypatch) -> None:
    _land_snapshot_bytes(tmp_path, RUN_DATE, 1_000_000)
    settings = _settings(tmp_path)

    _fake_usage(monkeypatch, free=1_000_000 * 100)  # 100 days
    assert audit.check_disk(settings, RUN_DATE)[0].status == audit.WARN

    _fake_usage(monkeypatch, free=1_000_000 * 30)   # 30 days
    assert audit.check_disk(settings, RUN_DATE)[0].status == audit.FAIL


def test_growth_ignores_partitions_after_the_audited_day(tmp_path: Path) -> None:
    """Today's partition is half-written; counting it doubles the runway."""
    from datetime import timedelta
    _land_snapshot_bytes(tmp_path, RUN_DATE, 1_000_000)
    _land_snapshot_bytes(tmp_path, RUN_DATE + timedelta(days=1), 10_000)  # in progress
    per_day, sampled = audit.daily_snapshot_growth(_settings(tmp_path), RUN_DATE)
    assert per_day == 1_000_000
    assert sampled == 1


def test_growth_uses_the_busiest_day_not_the_mean(tmp_path: Path) -> None:
    """A short day in the sample must not flatter the runway."""
    from datetime import timedelta
    _land_snapshot_bytes(tmp_path, RUN_DATE - timedelta(days=1), 10_000)  # part day
    _land_snapshot_bytes(tmp_path, RUN_DATE, 1_000_000)
    per_day, sampled = audit.daily_snapshot_growth(_settings(tmp_path), RUN_DATE)
    assert per_day == 1_000_000
    assert sampled == 2


# ---------------------------------------------------------------------------
# main() argv handling
# ---------------------------------------------------------------------------

def test_main_keeps_an_equals_style_date(monkeypatch) -> None:
    """argparse accepts ``--date=X``; a bare "--date" membership test misses
    that form and the appended default would silently audit the wrong day."""
    seen: dict = {}
    monkeypatch.setattr(audit, "run_job",
                        lambda job, fn, argv: seen.setdefault("argv", argv))
    audit.main(["--date=2026-08-28"])
    assert seen["argv"] == ["--date=2026-08-28"]


def test_main_defaults_date_to_the_previous_trading_day(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(audit, "run_job",
                        lambda job, fn, argv: seen.setdefault("argv", argv))
    audit.main([])
    assert seen["argv"][0] == "--date"


# ---------------------------------------------------------------------------
# Underlying history against the rolling entitlement window
#
# The equity aggregate endpoints only serve ~2 years back, so unlike the
# option flat files this history expires. The job ran faithfully every morning
# for T-1 while holding four days in total, and nothing noticed.
# ---------------------------------------------------------------------------

def _land_underlying(root: Path, dataset: str, days: list[date]) -> None:
    for d in days:
        part = root / "clean" / dataset / f"dt={d.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        (part / "underlying_bars-SPY-1.parquet").write_bytes(b"")


def _sessions(settings: Settings, start: date, end: date) -> list[date]:
    from datetime import timedelta

    from ingest.common import market_gate

    out, d = [], start
    while d <= end:
        if market_gate.is_trading_day(d, settings.data_root):
            out.append(d)
        d += timedelta(days=1)
    return out


def _full_window(settings: Settings, d: date) -> list[date]:
    from datetime import timedelta

    start = d - timedelta(days=audit.UNDERLYING_ENTITLEMENT_DAYS)
    return _sessions(settings, start, d)


def test_underlying_window_passes_when_the_window_is_complete(tmp_path) -> None:
    s = _settings(tmp_path)
    days = _full_window(s, RUN_DATE)
    for ds in audit.UNDERLYING_DATASETS:
        _land_underlying(tmp_path, ds, days)
    checks = {c.name: c for c in audit.check_underlying_window(s, RUN_DATE)}
    for ds in audit.UNDERLYING_DATASETS:
        assert checks[f"{ds}_window"].status == audit.PASS, checks[f"{ds}_window"].detail
        assert checks[f"{ds}[{RUN_DATE}]"].status == audit.PASS


def test_missing_yesterday_fails_even_with_a_full_history(tmp_path) -> None:
    """The daily job breaking is a different failure from an old hole."""
    s = _settings(tmp_path)
    days = [d for d in _full_window(s, RUN_DATE) if d != RUN_DATE]
    for ds in audit.UNDERLYING_DATASETS:
        _land_underlying(tmp_path, ds, days)
    checks = {c.name: c for c in audit.check_underlying_window(s, RUN_DATE)}
    assert checks[f"underlying_minute_bars[{RUN_DATE}]"].status == audit.FAIL


def test_a_hole_in_the_middle_warns_because_it_is_still_fetchable(tmp_path) -> None:
    from datetime import timedelta

    s = _settings(tmp_path)
    window = _full_window(s, RUN_DATE)
    # Drop a session comfortably inside the window, away from the old edge.
    victim = window[len(window) // 2]
    assert victim > window[0] + timedelta(days=audit.UNDERLYING_EDGE_DAYS)
    for ds in audit.UNDERLYING_DATASETS:
        _land_underlying(tmp_path, ds, [d for d in window if d != victim])
    checks = {c.name: c for c in audit.check_underlying_window(s, RUN_DATE)}
    got = checks["underlying_minute_bars_window"]
    assert got.status == audit.WARN
    assert got.data["missing"] == 1 and got.data["expiring"] == 0
    assert "backfill_underlying.sh" in got.detail


def test_a_hole_about_to_expire_fails(tmp_path) -> None:
    """Inside the edge zone, "later" has stopped being an option."""
    s = _settings(tmp_path)
    window = _full_window(s, RUN_DATE)
    victim = window[0]
    for ds in audit.UNDERLYING_DATASETS:
        _land_underlying(tmp_path, ds, [d for d in window if d != victim])
    checks = {c.name: c for c in audit.check_underlying_window(s, RUN_DATE)}
    got = checks["underlying_minute_bars_window"]
    assert got.status == audit.FAIL
    assert got.data["expiring"] == 1
    assert "unrecoverable" in got.detail


def test_window_does_not_reach_past_the_entitlement_boundary(tmp_path) -> None:
    """Hunting past ~2 years would report expected 403s as gaps forever."""
    from datetime import timedelta

    s = _settings(tmp_path)
    _land_underlying(tmp_path, "underlying_minute_bars", _full_window(s, RUN_DATE))
    checks = {c.name: c for c in audit.check_underlying_window(s, RUN_DATE)}
    got = checks["underlying_minute_bars_window"]
    assert got.status == audit.PASS
    start = date.fromisoformat(got.data["window_start"])
    assert RUN_DATE - start == timedelta(days=audit.UNDERLYING_ENTITLEMENT_DAYS)
    assert start > RUN_DATE - timedelta(days=365 * 2)


def test_underlying_window_is_part_of_the_daily_run(tmp_path) -> None:
    s = _settings(tmp_path)
    names = {c.name for c in audit.run_checks(s, RUN_DATE, _logger())}
    assert any(n.endswith("_window") for n in names), names
