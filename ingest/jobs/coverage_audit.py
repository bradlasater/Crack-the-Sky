"""coverage_audit: did we actually capture everything for a given day?

Every other job in this repo reports on itself. Nothing reported on the
*absence* of a job, which is how a dataset can go missing for weeks while
every log line looks healthy -- the SPX/SPXW hole in ``trades_watchlist`` and
the websocket job that had never once produced a file were both invisible for
exactly this reason.

This job asserts expectations for one trading day and exits non-zero when any
of them fail, so cron, Healthchecks.io and the box CI workflow all surface it:

  * ``option_snapshots`` -- sweeps landed vs. sweeps the schedule implies
    (derived from ``market_gate``, not hardcoded, so early closes are
    handled), plus the largest gap between consecutive sweeps.
  * flat files -- all three datasets present in the manifest with rows kept.
  * ``contracts`` -- universe present, and per-underlying counts sane.
  * ``option_trades`` / bars -- partitions non-empty.
  * websocket capture -- raw files present and ``ws_gap`` events counted.
  * per-underlying ticker coverage -- so an SPX-shaped hole cannot again look
    like a healthy run.

Run: ``python -m ingest.jobs.coverage_audit [--date YYYY-MM-DD]``
(default: the previous trading day).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ingest.common import landing, market_gate
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import OPTION_ROOTS, _clean_root, ticker_root, underlying_root

JOB = "coverage_audit"
COVERAGE_NAME = "coverage.json"

# Sweep cadence the crontab installs (one per minute during the session).
SWEEP_INTERVAL_S = 60
# Fraction of expected sweeps below which the day is a FAIL.
SWEEP_MIN_RATIO = 0.95
# Largest tolerated hole between consecutive sweeps.
MAX_SWEEP_GAP_S = 180
# Underlying roots we expect on every trading day.
EXPECTED_ROOTS = ("SPY", "SPX")
# Flat-file datasets flatfile_pull is responsible for.
FLATFILE_DATASETS = ("trades_v1", "minute_aggs_v1", "day_aggs_v1")

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class Check:
    """One assertion about a day's captured data."""

    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


def expected_sweeps(d: date, data_root: Path | str | None = None) -> int:
    """Sweeps the 1-minute schedule should produce on ``d``.

    Window is 09:30 ET to the option capture end (market close + 20 min), so
    a 13:00 early close yields ~241 instead of ~421 without special-casing.
    """
    open_et = datetime.combine(d, market_gate.time(9, 30), tzinfo=market_gate.ET)
    end_et = market_gate.option_capture_end_et(d, data_root)
    minutes = int((end_et - open_et).total_seconds() // SWEEP_INTERVAL_S)
    return max(minutes, 0)


def _sweep_stamps(settings: Settings, d: date) -> dict[str, list[int]]:
    """Sweep epoch-ms stamps per underlying root from the clean partition."""
    part = _clean_root(settings, "option_snapshots") / f"dt={d.isoformat()}"
    out: dict[str, list[int]] = {}
    for path in part.glob("*.parquet"):
        parts = path.stem.rsplit("-", 2)
        if len(parts) != 3:
            continue
        _label, underlying, epoch = parts
        try:
            out.setdefault(underlying_root(underlying), []).append(int(epoch))
        except ValueError:
            continue
    return {root: sorted(v) for root, v in out.items()}


def check_snapshots(settings: Settings, d: date) -> list[Check]:
    """Snapshot cadence and continuity -- the irreplaceable dataset."""
    stamps = _sweep_stamps(settings, d)
    expected = expected_sweeps(d, settings.data_root)
    checks: list[Check] = []
    for root in EXPECTED_ROOTS:
        got = stamps.get(root, [])
        if not got:
            checks.append(Check(
                f"snapshots[{root}]", FAIL,
                f"no sweeps landed (expected ~{expected})",
                {"sweeps": 0, "expected": expected},
            ))
            continue
        ratio = len(got) / expected if expected else 1.0
        gaps = [
            (got[i + 1] - got[i]) / 1000.0 for i in range(len(got) - 1)
        ]
        max_gap = max(gaps) if gaps else 0.0
        status = PASS
        notes = []
        if ratio < SWEEP_MIN_RATIO:
            status = FAIL
            notes.append(f"only {ratio:.0%} of expected sweeps")
        if max_gap > MAX_SWEEP_GAP_S:
            status = FAIL if status == FAIL else WARN
            notes.append(f"largest gap {max_gap:.0f}s")
        checks.append(Check(
            f"snapshots[{root}]", status,
            f"{len(got)}/{expected} sweeps"
            + (f" -- {'; '.join(notes)}" if notes else ""),
            {"sweeps": len(got), "expected": expected,
             "ratio": round(ratio, 4), "max_gap_s": round(max_gap, 1)},
        ))
    return checks


def check_flatfiles(settings: Settings, d: date) -> list[Check]:
    """All three flat-file datasets pulled, with rows kept."""
    path = landing.meta_path("flatfile_manifest.json", data_root=settings.data_root)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        manifest = []
    day = d.isoformat()
    entries = {
        e["dataset"]: e for e in manifest
        if isinstance(e, dict) and e.get("date") == day
    }
    checks: list[Check] = []
    for dataset in FLATFILE_DATASETS:
        entry = entries.get(dataset)
        if entry is None:
            checks.append(Check(
                f"flatfile[{dataset}]", FAIL, "not in manifest", {}))
        elif not entry.get("rows_kept"):
            checks.append(Check(
                f"flatfile[{dataset}]", FAIL,
                f"pulled but kept 0 rows (rows_in={entry.get('rows_in')})",
                dict(entry)))
        else:
            checks.append(Check(
                f"flatfile[{dataset}]", PASS,
                f"{entry['rows_kept']:,} rows kept of {entry.get('rows_in', 0):,}",
                {"rows_kept": entry["rows_kept"], "rows_in": entry.get("rows_in")}))
    return checks


def _partition_rows(settings: Settings, dataset: str, d: date) -> int:
    """Total rows across a clean partition (0 when absent)."""
    try:
        import pyarrow.parquet as pq
    except ImportError:  # pragma: no cover
        return 0
    part = _clean_root(settings, dataset) / f"dt={d.isoformat()}"
    total = 0
    for path in part.glob("*.parquet"):
        try:
            total += pq.ParquetFile(path).metadata.num_rows
        except Exception:  # noqa: BLE001 - a corrupt file is a finding, not a crash
            return -1
    return total


def check_partitions(settings: Settings, d: date) -> list[Check]:
    """Clean partitions that should be non-empty for a trading day."""
    checks = []
    for dataset in ("contracts", "option_trades", "option_minute_bars",
                    "option_day_bars", "forwards"):
        rows = _partition_rows(settings, dataset, d)
        if rows < 0:
            checks.append(Check(f"partition[{dataset}]", FAIL,
                                "unreadable parquet in partition", {}))
        elif rows == 0:
            checks.append(Check(f"partition[{dataset}]", FAIL,
                                "partition missing or empty", {"rows": 0}))
        else:
            checks.append(Check(f"partition[{dataset}]", PASS,
                                f"{rows:,} rows", {"rows": rows}))
    return checks


def check_underlying_coverage(settings: Settings, d: date) -> list[Check]:
    """Per-underlying ticker counts, so a one-sided hole cannot hide.

    This is the check that would have caught the SPY-derived strike band
    being applied to SPX: the run logged 2,658 tickers and looked fine, but
    only 2 of them were SPX.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError:  # pragma: no cover
        return [Check("underlying_coverage", SKIP, "pyarrow unavailable", {})]

    part = _clean_root(settings, "option_trades") / f"dt={d.isoformat()}"
    counts: dict[str, int] = {}
    for path in part.glob("*.parquet"):
        try:
            tickers = pq.read_table(path, columns=["ticker"]).column("ticker").to_pylist()
        except Exception:  # noqa: BLE001
            continue
        for ticker in tickers:
            root = ticker_root(ticker)
            if root:
                counts[root] = counts.get(root, 0) + 1

    if not counts:
        return [Check("underlying_coverage", FAIL,
                      "no option_trades rows to attribute", {})]

    # SPXW carries ~98% of SPX option trades (measured on the 2026-08-28 flat
    # file: 1,760,084 SPXW vs 33,951 SPX), so its absence means the SPX side
    # is effectively empty regardless of how healthy the row count looks.
    checks = [Check("underlying_coverage", PASS,
                    ", ".join(f"{k}={v:,}" for k, v in sorted(counts.items())),
                    dict(counts))]
    for root in OPTION_ROOTS:
        if counts.get(root, 0) == 0:
            checks.append(Check(f"underlying[{root}]", FAIL,
                                "zero trades captured for this root", {}))

    # Roots outside OPTION_ROOTS mean the ticker filter is admitting other
    # underlyings (SPXL/SPXS/SPYG are leveraged ETFs, not SPY or SPX).
    foreign = {k: v for k, v in counts.items() if k not in OPTION_ROOTS}
    if foreign:
        checks.append(Check(
            "ticker_purity", FAIL,
            "foreign roots in option_trades: "
            + ", ".join(f"{k}={v:,}" for k, v in sorted(foreign.items())),
            dict(foreign)))
    else:
        checks.append(Check("ticker_purity", PASS,
                            f"only {'/'.join(OPTION_ROOTS)} present", {}))
    return checks


def check_websocket(settings: Settings, d: date, logger: JsonlLogger) -> list[Check]:
    """Websocket capture produced files, and how many reconnect gaps."""
    raw = Path(settings.data_root) / "raw" / "option_minute_bars_ws" / f"dt={d.isoformat()}"
    files = sorted(raw.glob("*.jsonl*")) if raw.is_dir() else []
    total = sum(f.stat().st_size for f in files)

    log_dir = Path(settings.log_root) / "ws_minute_bars" / f"dt={d.isoformat()}"
    gaps = 0
    if log_dir.is_dir():
        for path in log_dir.glob("*.log"):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if '"ws_gap"' in line:
                    gaps += 1

    if not files:
        return [Check("websocket", FAIL,
                      "no capture files -- the WS job did not run or wrote nothing",
                      {"files": 0, "bytes": 0, "ws_gap_events": gaps})]
    status = WARN if gaps > 5 else PASS
    return [Check("websocket", status,
                  f"{len(files)} files, {total / 1e6:.1f} MB, {gaps} ws_gap events",
                  {"files": len(files), "bytes": total, "ws_gap_events": gaps})]


def run_checks(settings: Settings, d: date, logger: JsonlLogger) -> list[Check]:
    """Every check for one trading day."""
    checks: list[Check] = []
    checks += check_snapshots(settings, d)
    checks += check_flatfiles(settings, d)
    checks += check_partitions(settings, d)
    checks += check_underlying_coverage(settings, d)
    checks += check_websocket(settings, d, logger)
    return checks


def _render(d: date, checks: list[Check]) -> str:
    """PASS/FAIL table, in the style of ``ingest.smoke``."""
    width = max(len(c.name) for c in checks) if checks else 10
    lines = [f"coverage_audit -- {d.isoformat()}", "-" * (width + 60)]
    for c in checks:
        lines.append(f"{c.status:<5} {c.name:<{width}}  {c.detail}")
    counts: dict[str, int] = {}
    for c in checks:
        counts[c.status] = counts.get(c.status, 0) + 1
    lines.append("-" * (width + 60))
    lines.append("  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return "\n".join(lines)


class CoverageError(RuntimeError):
    """Raised when any check fails, so run_job exits 1 and pings /fail."""


def _main_fn(args, settings: Settings, logger: JsonlLogger):
    d = date.fromisoformat(args.date)
    checks = run_checks(settings, d, logger)

    for c in checks:
        logger.log("coverage", check=c.name, status=c.status, detail=c.detail, **c.data)
    print(_render(d, checks), file=sys.stderr)

    payload = {
        "date": d.isoformat(),
        "generated_at": market_gate.now_et().isoformat(),
        "checks": [
            {"name": c.name, "status": c.status, "detail": c.detail, **c.data}
            for c in checks
        ],
    }
    if not args.dry_run:
        path = landing.meta_path(COVERAGE_NAME, data_root=settings.data_root)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    failures = [c for c in checks if c.status == FAIL]
    summary = {
        "rows": len(checks),
        "date": d.isoformat(),
        "failed": len(failures),
        "warned": len([c for c in checks if c.status == WARN]),
    }
    if failures:
        raise CoverageError(
            f"{len(failures)} coverage check(s) failed for {d}: "
            + "; ".join(f"{c.name}: {c.detail}" for c in failures)
        )
    return summary


def main(argv: list[str] | None = None) -> None:
    """Entry point; defaults --date to the previous trading day, then run_job.

    The date must be resolved before ``run_job``: the audit runs Tue-Sat to
    grade the prior session, and ``run_job``'s market gate would otherwise
    exit 0 on the Saturday run.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--date" not in argv:
        prev = market_gate.previous_trading_day(market_gate.today_et())
        argv += ["--date", prev.isoformat()]
    run_job(JOB, _main_fn, argv)


if __name__ == "__main__":
    main()
