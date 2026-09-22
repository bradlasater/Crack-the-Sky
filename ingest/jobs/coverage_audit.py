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
  * ``vol_surface`` -- T-1 SVI fit landed for every scheduled root
    (derived; a silent skip is as invisible as a capture hole).
  * the par curve -- how many trading days behind the session the newest
    ``treasury_yields`` row is, since a rates job that stops landing is
    otherwise silent: every IV still inverts, just off a frozen curve.
  * websocket capture -- raw files present and ``ws_gap`` events counted.
  * disk runway -- how many days of snapshot growth the volume still holds.
  * per-underlying ticker coverage -- so an SPX-shaped hole cannot again look
    like a healthy run.

Run: ``python -m ingest.jobs.coverage_audit [--date YYYY-MM-DD]``
(default: the last completed session -- T-1 once the following day's pipeline
has produced it, which is what ``last_completed_session`` works out).
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from ingest.common import landing, market_gate
from ingest.common.cli import run_job
from ingest.common.config import Settings, default_data_root
from ingest.common.logging_utils import JsonlLogger
from ingest.common.rates import DATASET as RATE_DATASET
from ingest.jobs import OPTION_ROOTS, ticker_root, underlying_root
from ingest.jobs.flatfile_pull import CLEAN_DATASET as FLATFILE_CLEAN
from ingest.jobs.flatfile_pull import JOB as FLATFILE_JOB

# Same tuple as pricing.surface.SURFACE_ROOTS. Duplicated so this module
# does not import pricing; test_coverage_audit pins the two equal.
SURFACE_ROOTS = ("SPX", "SPXW")

JOB = "coverage_audit"
COVERAGE_NAME = "coverage.json"

# When the previous session's pipeline has finished producing, in ET. The
# surface job is the last link in it (deploy/schedule.json: Tue-Sat 12:15),
# and this audit's own slot is the 12:30 right behind it. Before this time a
# session's partitions are not late, they are simply not due -- which is the
# distinction last_completed_session() exists to draw.
PIPELINE_DONE_ET = time(12, 15)


def _clean_root(settings: Settings, dataset: str) -> Path:
    return Path(settings.data_root) / "clean" / dataset

# Sweep cadence the schedule installs (one per minute during the session).
SWEEP_INTERVAL_S = 60
# Fraction of expected sweeps below which the day is a FAIL.
SWEEP_MIN_RATIO = 0.95
# Largest tolerated hole between consecutive sweeps.
MAX_SWEEP_GAP_S = 180

# The schedule (deploy/schedule.json) schedules three *different* things into
# one partition, and conflating them is what made this check useless: it
# reported a WARN on SPY and SPX every single day, on a healthy box.
#
#   05 09            one pre-open sweep, for the prior session's settled OI
#   30-59 9 / 10-15 / 0-30 16   the continuous 1-minute cadence
#   35 16            one EOD sweep
#
# The 09:05 -> 09:30 wait (1500s) and the 16:30 -> 16:35 wait (300s) are the
# schedule working as designed, but both blow MAX_SWEEP_GAP_S, so a
# whole-partition gap scan can only ever cry wolf. Any sweep run by hand
# outside the session lands in the same partition too, which is how one
# afternoon of manual runs produced a reported "29,493s gap".
#
# So: the cadence numbers below are computed over the continuous window only,
# and the two deliberate singletons are asserted separately.
#
# Early closes are the audit's to own (owner decision 2026-09; the schedule
# stays as installed). Neither cron nor an OnCalendar expression can state the
# NYSE calendar, so on a 13:00 close the cadence lines keep firing to 16:30 and
# those sweeps land in the same partition. sweep_window ends the canonical window at the actual
# session close via market_gate.market_close_et, and _classify_stamps puts
# the post-close firings in their own bucket rather than reading ~178 of them
# as "stray". The early-close answer comes from market_gate reading
# _meta/holidays.json directly -- the same underlying file pricing.calendar
# unions into its session calendar -- because the import direction in this
# repo is pricing -> ingest, never the reverse (see SURFACE_ROOTS above).
SWEEP_WINDOW_OPEN_ET = time(9, 30)
# Continuous cadence runs to close + 30 min (the schedule's "0-30 16" line).
# This is deliberately *not* market_gate.option_capture_end_et (close + 35):
# that is the websocket job's deadline, sized for when the delayed feed
# *delivers* the last bar, and has nothing to do with when the sweep stops
# firing. The
# two were briefly the same number, which is how borrowing it here once
# understated the expected count by ten sweeps a day.
SWEEP_TAIL = timedelta(minutes=30)
# A stamp is the moment the sweep *wrote*, not the moment cron fired it, and a
# full two-chain sweep takes ~14s. Without this the 16:30 sweep lands at
# 16:30:13, outside a window ending at 16:30:00, and gets miscounted as the
# EOD singleton.
SWEEP_WRITE_GRACE = timedelta(minutes=2)
# The two scheduled singletons, with a tolerance either side for cron jitter
# and sweep duration.
PREOPEN_SWEEP_ET = time(9, 5)
EOD_SWEEP_ET = time(16, 35)
SINGLETON_TOLERANCE = timedelta(minutes=10)
# Underlying roots we expect on every trading day.
EXPECTED_ROOTS = ("SPY", "SPX", "VIX")
# Flat-file datasets flatfile_pull is responsible for.
FLATFILE_DATASETS = ("trades_v1", "minute_aggs_v1", "day_aggs_v1")

# Datasets whose clean partition flatfile_pull still owns, and so the only
# ones the manifest row count can be checked against. minute_aggs_v1 is
# absent on purpose: reconcile rewrites option_minute_bars from the flat file
# and quarantines flatfile_pull's parquet as it goes, so that partition
# legitimately holds no flatfile_pull-written file at all. Duplication there
# is already prevented by reconcile's own quarantine_prior.
FLATFILE_OWNED_PARTITIONS = ("trades_v1", "day_aggs_v1")

# How stale the par curve may be, measured in trading days between the audited
# session and the newest curve row at or before it.
#
# The vendor publishes at a steady T-2: across the twelve rates_sync runs from
# 2026-09-01 to 2026-09-18 the newest row was exactly 2 trading days behind the
# run date every single time (2 calendar days midweek, 4-5 across a weekend or
# Labor Day). So 2 is the healthy number and anything above it means a run did
# not land, not that the Treasury was slow.
#
# WARN at 3 catches one missed run on the morning after. FAIL at 6 is a job
# that has stopped: a whole week of sessions discounting off the same curve.
# The gap is counted in trading days rather than calendar days precisely so a
# weekend or a holiday does not read as staleness.
RATE_CURVE_WARN_TRADING_DAYS = 2
RATE_CURVE_FAIL_TRADING_DAYS = 5

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class Check:
    """One assertion about a day's captured data."""

    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


def sweep_window(
    d: date, data_root: Path | str | None = None
) -> tuple[datetime, datetime]:
    """The continuous 1-minute sweep window on ``d`` as ``(open, end)`` ET.

    09:30 to market close + :data:`SWEEP_TAIL`, so a 13:00 early close closes
    the window at 13:30 without special-casing.
    """
    open_et = datetime.combine(d, SWEEP_WINDOW_OPEN_ET, tzinfo=market_gate.ET)
    end_et = market_gate.market_close_et(d, data_root) + SWEEP_TAIL
    return open_et, end_et + SWEEP_WRITE_GRACE


def expected_sweeps(d: date, data_root: Path | str | None = None) -> int:
    """Sweeps the 1-minute schedule should produce inside the window on ``d``.

    Both endpoints fire (cron runs at 09:30 *and* at 16:30), so this is the
    number of minutes spanned plus one.
    """
    open_et, end_et = sweep_window(d, data_root)
    scheduled_span = (end_et - SWEEP_WRITE_GRACE) - open_et
    minutes = int(scheduled_span.total_seconds() // SWEEP_INTERVAL_S)
    return max(minutes + 1, 0) if minutes >= 0 else 0


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


def _classify_stamps(
    stamps: list[int], d: date, data_root: Path | str | None = None
) -> dict[str, list[int]]:
    """Split one root's sweep stamps by what the schedule intended them to be.

    ``window`` are the continuous 1-minute sweeps, and are the only ones the
    cadence and gap numbers may be computed from. ``preopen`` and ``eod`` are
    the two scheduled singletons. ``stray`` is everything else -- typically a
    sweep run by hand outside the session; reported, never counted.

    ``post_close`` is the early-close case. The schedule cannot state the NYSE
    calendar, so on a 13:00 close the cadence lines keep firing to 16:30 and
    ~178 sweeps land after the canonical window has ended. The audit owns
    early closes (the schedule deliberately stays put): those sweeps are the
    schedule working as installed, so they are accounted for separately
    rather than reported as strays -- and never asserted on, so a sweep job
    that learns to stop at the early close does not fail the day it ships.
    """
    open_et, end_et = sweep_window(d, data_root)
    preopen_at = datetime.combine(d, PREOPEN_SWEEP_ET, tzinfo=market_gate.ET)
    eod_at = datetime.combine(d, EOD_SWEEP_ET, tzinfo=market_gate.ET)
    # The cadence's hard stop on any day: regular close + tail + write grace.
    cadence_end = (
        datetime.combine(d, market_gate.REGULAR_CLOSE, tzinfo=market_gate.ET)
        + SWEEP_TAIL + SWEEP_WRITE_GRACE
    )
    early_close = d in market_gate.load_early_closes(data_root)
    out: dict[str, list[int]] = {
        "window": [], "preopen": [], "eod": [], "post_close": [], "stray": [],
    }
    for ms in stamps:
        at = datetime.fromtimestamp(ms / 1000.0, tz=market_gate.ET)
        if open_et <= at <= end_et:
            out["window"].append(ms)
        elif abs(at - preopen_at) <= SINGLETON_TOLERANCE:
            out["preopen"].append(ms)
        elif early_close and end_et < at <= cadence_end:
            # Before the EOD tolerance: the real EOD sweep runs at 16:35,
            # after cadence_end, so on an early close the still-firing
            # cadence (16:25-16:30) reaches the tolerance window first and
            # would otherwise stand in for a missing EOD run.
            out["post_close"].append(ms)
        elif abs(at - eod_at) <= SINGLETON_TOLERANCE:
            out["eod"].append(ms)
        else:
            out["stray"].append(ms)
    return {k: sorted(v) for k, v in out.items()}


def check_snapshots(settings: Settings, d: date) -> list[Check]:
    """Snapshot cadence and continuity -- the irreplaceable dataset."""
    stamps = _sweep_stamps(settings, d)
    expected = expected_sweeps(d, settings.data_root)
    checks: list[Check] = []
    missing_preopen: list[str] = []
    missing_eod: list[str] = []
    for root in EXPECTED_ROOTS:
        parts = _classify_stamps(stamps.get(root, []), d, settings.data_root)
        got = parts["window"]
        if not parts["preopen"]:
            missing_preopen.append(root)
        if not parts["eod"]:
            missing_eod.append(root)
        if not got:
            checks.append(Check(
                f"snapshots[{root}]", FAIL,
                f"no in-session sweeps landed (expected ~{expected})",
                {"sweeps": 0, "expected": expected,
                 "preopen": len(parts["preopen"]), "eod": len(parts["eod"]),
                 "post_close": len(parts["post_close"]),
                 "stray": len(parts["stray"])},
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
        if parts["stray"]:
            notes.append(f"{len(parts['stray'])} sweep(s) outside the schedule")
        checks.append(Check(
            f"snapshots[{root}]", status,
            f"{len(got)}/{expected} sweeps"
            + (f" -- {'; '.join(notes)}" if notes else ""),
            {"sweeps": len(got), "expected": expected,
             "ratio": round(ratio, 4), "max_gap_s": round(max_gap, 1),
             "preopen": len(parts["preopen"]), "eod": len(parts["eod"]),
             "post_close": len(parts["post_close"]),
             "stray": len(parts["stray"])},
        ))
    # The two singletons carry data the cadence cannot: the pre-open sweep is
    # the only capture of the prior session's settled open interest, and the
    # EOD sweep is what drift_check reprices. Losing either is silent
    # otherwise, because 421 healthy in-session sweeps say nothing about them.
    checks.append(Check(
        "snapshots_preopen",
        FAIL if missing_preopen else PASS,
        f"missing for {', '.join(missing_preopen)}" if missing_preopen
        else f"present for {', '.join(EXPECTED_ROOTS)}",
        {"missing": missing_preopen},
    ))
    checks.append(Check(
        "snapshots_eod",
        FAIL if missing_eod else PASS,
        f"missing for {', '.join(missing_eod)}" if missing_eod
        else f"present for {', '.join(EXPECTED_ROOTS)}",
        {"missing": missing_eod},
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
        # Only meaningful once the job claims it wrote something: at
        # rows_kept 0 the flatfile[...] check above already failed, and a
        # second failure saying the same thing is noise.
        if dataset in FLATFILE_OWNED_PARTITIONS and entry and entry.get("rows_kept"):
            checks.append(_check_flatfile_partition(settings, dataset, entry, d))
    return checks


def _flatfile_partition_rows(
    settings: Settings, clean_dataset: str, d: date
) -> tuple[int, int]:
    """``(rows, files)`` that flatfile_pull wrote into a clean partition.

    Scoped to this job's own file names, because other jobs land in the same
    partitions -- trades_watchlist writes ~90 files a day into option_trades
    -- and their rows are not what the manifest counted. ``(-1, -1)`` when a
    file will not open.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError:  # pragma: no cover
        return 0, 0
    part = _clean_root(settings, clean_dataset) / f"dt={d.isoformat()}"
    total = files = 0
    for path in sorted(part.glob(f"{FLATFILE_JOB}-*.parquet")):
        try:
            total += pq.ParquetFile(path).metadata.num_rows
        except Exception:  # noqa: BLE001 - a corrupt file is a finding, not a crash
            return -1, -1
        files += 1
    return total, files


def _check_flatfile_partition(
    settings: Settings, dataset: str, entry: dict[str, Any], d: date
) -> Check:
    """The partition holds exactly the rows the manifest says were written.

    ``partition[...]`` only asks whether a partition is non-empty, and a
    double-written one passes that easily: 2026-09-04 and 2026-09-14 each
    carried every flat-file row twice while every check stayed green, for
    twelve days and four days respectively. Nothing downstream errored --
    a whole-partition read just returned each trade twice.

    The manifest records ``rows_kept`` per dataset and flatfile_pull writes
    exactly one file per dataset per date, so this is an exact invariant
    rather than a plausibility heuristic: more rows than that means a second
    write, fewer means output that was lost or truncated after the fact.
    """
    name = f"flatfile_partition[{dataset}]"
    kept = entry.get("rows_kept") or 0
    rows, files = _flatfile_partition_rows(settings, FLATFILE_CLEAN[dataset], d)
    data = {"rows": rows, "files": files, "rows_kept": kept}
    if rows < 0:
        return Check(name, FAIL, "unreadable parquet in partition", {"rows_kept": kept})
    if files == 0:
        return Check(name, FAIL,
                     f"manifest kept {kept:,} rows but no {FLATFILE_JOB} file is there",
                     data)
    if rows == kept:
        return Check(name, PASS, f"{rows:,} rows in 1 file" if files == 1
                     else f"{rows:,} rows across {files} files", data)
    if files > 1 and kept and rows == kept * files:
        return Check(name, FAIL,
                     f"{files} copies of the same {kept:,} rows -- duplicate write",
                     data)
    return Check(name, FAIL,
                 f"{rows:,} rows in {files} file(s), manifest kept {kept:,}", data)


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


def _partition_underlyings(settings: Settings, dataset: str, d: date) -> set[str] | None:
    """Distinct ``underlying`` values in a clean partition; None if unreadable."""
    try:
        import pyarrow.parquet as pq
    except ImportError:  # pragma: no cover
        return set()
    part = _clean_root(settings, dataset) / f"dt={d.isoformat()}"
    found: set[str] = set()
    for path in part.glob("*.parquet"):
        try:
            values = pq.read_table(path, columns=["underlying"]).column("underlying").to_pylist()
        except Exception:  # noqa: BLE001 - a corrupt file is a finding, not a crash
            return None
        found.update(str(v) for v in values if v is not None)
    return found


def check_vol_surface(settings: Settings, d: date) -> list[Check]:
    """Yesterday's fitted smile landed -- a silent skip is as invisible as a capture hole.

    Derived, so a missed day is rebuilt with ``scripts/build_surface.py``
    rather than lost. It is still a FAIL when the partition is missing or
    when a scheduled root is absent: ``build_surfaces`` omits a root with
    no chain, so a nonempty SPXW-only partition would pass a row-count
    check while ``load_surface`` fails for SPX.
    """
    rows = _partition_rows(settings, "vol_surface", d)
    if rows < 0:
        return [Check("vol_surface", FAIL, "unreadable parquet in partition", {})]
    if rows == 0:
        return [Check("vol_surface", FAIL, "partition missing or empty", {"rows": 0})]
    roots = _partition_underlyings(settings, "vol_surface", d)
    if roots is None:
        return [Check("vol_surface", FAIL, "unreadable parquet in partition", {})]
    missing = [r for r in SURFACE_ROOTS if r not in roots]
    if missing:
        return [Check(
            "vol_surface", FAIL,
            f"missing roots {missing}",
            {"rows": rows, "roots": sorted(roots), "missing": missing},
        )]
    return [Check("vol_surface", PASS, f"{rows:,} slices", {"rows": rows})]


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


# The equity aggregate endpoints are entitled only inside a ROLLING window,
# unlike the option flat files, which go back to 2022 and stay there. Probed
# 2026-09-03: SPY 1-minute aggs return 200 for 2024-09-03 and 403 "Your plan
# doesn't include this data timeframe" for 2024-06-03.
#
# That makes underlying history the second dataset on this box that expires.
# option_snapshots is the obvious one and is watched everywhere; this one is
# quieter and was missed completely -- the job ran faithfully every morning
# for T-1 and nobody noticed it held four days in total, while two years of
# fetchable sessions aged off the far edge unclaimed.
#
# Measured at 2 years; held slightly short so the check does not itself go
# hunting past the boundary and call an expected 403 a gap.
UNDERLYING_ENTITLEMENT_DAYS = 365 * 2 - 7
# Sessions this close to falling out of the window are the last chance to
# fetch them, so a hole there is a FAIL rather than a WARN.
UNDERLYING_EDGE_DAYS = 30
UNDERLYING_DATASETS = ("underlying_minute_bars", "underlying_day_bars")


def _window_sessions(settings: Settings, start: date, end: date) -> list[date]:
    """Real sessions in ``[start, end]``, per the vendor's own record.

    Deliberately not ``market_gate.is_trading_day``: holidays.json is fed by
    /v1/marketstatus/**upcoming**, so it knows nothing about past holidays and
    the gate fails open on every historical weekday. Auditing against it would
    report Christmas 2024 as a permanently missing session and this check
    would never reach PASS. flatfile_pull's manifest lists the dates the
    vendor actually published trades_v1 for, which is the same oracle
    history_audit uses.
    """
    from ingest.jobs.flatfile_pull import manifest_dates

    return sorted(
        d for d in (
            date.fromisoformat(x)
            for x in manifest_dates(Path(settings.data_root))
        )
        if start <= d <= end
    )


def _missing_sessions(
    settings: Settings, dataset: str, sessions: list[date]
) -> list[date]:
    """Sessions with no clean partition for ``dataset``."""
    root = _clean_root(settings, dataset)
    out = []
    for day in sessions:
        part = root / f"dt={day.isoformat()}"
        if not part.is_dir() or not any(part.glob("*.parquet")):
            out.append(day)
    return out


def check_underlying_window(
    settings: Settings, d: date, today: date | None = None
) -> list[Check]:
    """Underlying history, against the window the plan will still serve.

    Two questions, because they fail differently. Did yesterday's scheduled
    run land? And is any of the still-fetchable history missing -- with what
    is about to expire called out separately, since that is the part where
    "later" stops being an option.
    """
    checks: list[Check] = []
    # Measured from the vendor's clock, not from the audited day. They differ
    # by one day in production (this runs on T-1) but not when the job is
    # pointed at an older date by hand, and a boundary that slides with the
    # question being asked is the same bug the backfill had: it would report
    # long-expired sessions as still fetchable.
    today = today or market_gate.today_et()
    window_start = today - timedelta(days=UNDERLYING_ENTITLEMENT_DAYS)
    if window_start > d:
        return [Check("underlying_window", SKIP,
                      f"audited day {d.isoformat()} is older than the "
                      f"entitlement boundary {window_start.isoformat()}", {})]
    edge_end = window_start + timedelta(days=UNDERLYING_EDGE_DAYS)
    sessions = _window_sessions(settings, window_start, d)
    if not sessions:
        return [Check("underlying_window", SKIP,
                      "no flat-file manifest to enumerate sessions from", {})]

    for dataset in UNDERLYING_DATASETS:
        part = _clean_root(settings, dataset) / f"dt={d.isoformat()}"
        if part.is_dir() and any(part.glob("*.parquet")):
            checks.append(Check(f"{dataset}[{d}]", PASS, "session captured", {}))
        else:
            checks.append(Check(f"{dataset}[{d}]", FAIL,
                                "no partition -- the daily run did not land", {}))

        missing = _missing_sessions(settings, dataset, sessions)
        expiring = [m for m in missing if m <= edge_end]
        total = len(missing)
        detail = (f"{total} missing of {len(sessions)} sessions in the "
                  f"fetchable window {window_start.isoformat()}..{d.isoformat()}")
        data = {"missing": total, "expiring": len(expiring),
                "sessions": len(sessions),
                "window_start": window_start.isoformat()}
        if expiring:
            checks.append(Check(
                f"{dataset}_window", FAIL,
                f"{detail}; {len(expiring)} of them expire within "
                f"{UNDERLYING_EDGE_DAYS} days (oldest {expiring[0].isoformat()}) "
                "-- these are unrecoverable once they age out",
                data))
        elif total:
            checks.append(Check(f"{dataset}_window", WARN,
                                detail + " -- still fetchable, backfill with "
                                "scripts/backfill_underlying.py", data))
        else:
            checks.append(Check(f"{dataset}_window", PASS,
                                f"complete back to {window_start.isoformat()}", data))
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


# Disk runway thresholds, in days of continued snapshot growth.
#
# option_snapshots is the one dataset that must never stop, it runs ~1.7 GB a
# day, and prune_raw.sh correctly refuses to touch it -- so the volume filling
# up is a capture outage with a long fuse. The fuse is the thing to monitor:
# working the runway out once, by hand, is not monitoring it.
DISK_WARN_DAYS = 180
DISK_FAIL_DAYS = 60
# Partitions sampled to estimate daily growth. Enough to smooth a short
# session or a half-captured day, few enough to stay cheap.
DISK_SAMPLE_PARTITIONS = 5


def _partition_bytes(part: Path) -> int:
    return sum(f.stat().st_size for f in part.glob("*.parquet") if f.is_file())


def daily_snapshot_growth(settings: Settings, through: date) -> tuple[float, int]:
    """Bytes/day of ``option_snapshots`` growth, and how many days were sampled.

    Partitions after ``through`` are excluded because the audit runs at 12:30
    against T-1: today's partition is still being written, and including it
    would halve the estimate and so overstate the runway.

    The busiest sampled day is used rather than the mean. A runway estimate
    should err towards alarming early, and the sample legitimately contains
    short days -- the first day of capture, an early close -- that would
    otherwise flatter the number.
    """
    root = _clean_root(settings, "option_snapshots")
    if not root.is_dir():
        return 0.0, 0
    parts = sorted(
        (p for p in root.glob("dt=*") if p.is_dir() and p.name[3:] <= through.isoformat()),
        key=lambda p: p.name,
    )[-DISK_SAMPLE_PARTITIONS:]
    sizes = [b for b in (_partition_bytes(p) for p in parts) if b > 0]
    if not sizes:
        return 0.0, 0
    return float(max(sizes)), len(sizes)


def check_disk(settings: Settings, d: date) -> list[Check]:
    """Days of runway left on the warehouse volume at current growth."""
    try:
        usage = shutil.disk_usage(Path(settings.data_root))
    except OSError as exc:
        return [Check("disk_runway", FAIL,
                      f"cannot stat {settings.data_root}: {exc}", {})]

    per_day, sampled = daily_snapshot_growth(settings, d)
    free_gb = usage.free / 1e9
    data = {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "snapshot_bytes_per_day": round(per_day),
        "sampled_partitions": sampled,
    }
    if per_day <= 0:
        return [Check("disk_runway", SKIP,
                      f"{free_gb:,.0f} GB free -- no growth sample yet", data)]
    days = usage.free / per_day
    data["days_remaining"] = round(days, 1)
    status = PASS
    if days < DISK_FAIL_DAYS:
        status = FAIL
    elif days < DISK_WARN_DAYS:
        status = WARN
    return [Check(
        "disk_runway", status,
        f"{free_gb:,.0f} GB free -- {days:,.0f} days at "
        f"{per_day / 1e9:.2f} GB/day of snapshots",
        data,
    )]


def check_rate_curve(settings: Settings, d: date) -> list[Check]:
    """Is the discount curve the day was priced against actually current?

    Nothing else here notices a stale curve. ``rates_sync`` reports success on
    a run that lands rows, and every downstream consumer calls
    ``ingest.common.rates.load_curve``, which takes the newest row at or before
    the session and is perfectly happy to return one from last week. So a job
    that stops landing is silent: IVs keep inverting, the surface keeps
    fitting, and every number is quietly discounted off a curve that has
    stopped moving.

    That is not hypothetical. ``rates_sync`` was scheduled Tue-Sat while
    ``run_job``'s gate tests *today*, so every Saturday fire exited 0 without
    landing anything, and with no Monday run either, 2026-09-21 was priced off
    the 2026-09-16 curve -- 3 trading days back, with nothing anywhere saying
    so. The schedule is Mon-Fri now; this check is what makes the next
    regression of that shape loud instead.

    Reads the parquet directly rather than through ``rates.load_curve``: the
    audit wants the date on the newest row, and ``load_curve`` returns a
    ``RateCurve`` built from it. Going to the files also keeps this honest if
    the loader's own selection rule ever drifts.
    """
    import pyarrow.parquet as pq

    root = _clean_root(settings, RATE_DATASET)
    if not root.is_dir():
        return [Check("rate_curve", FAIL,
                      f"no {RATE_DATASET} data under {root} -- "
                      "every IV inversion is running on a fallback rate", {})]

    want = d.isoformat()
    newest: str | None = None
    # Every partition, because `dt=` is the ingestion run date, not the curve
    # date -- a resumed `--full` walk writes 1962 into the newest partition.
    # Same reason ingest.common.rates._load_curve_cached scans them all.
    for part in sorted(root.glob("dt=*")):
        for path in sorted(part.glob("*.parquet")):
            try:
                dates = pq.read_table(path, columns=["date"]).column("date").to_pylist()
            except Exception:  # noqa: BLE001 - a corrupt file is a finding, not a crash
                return [Check("rate_curve", FAIL,
                              f"unreadable parquet in {part.name}", {})]
            for row in dates:
                s = str(row or "")
                if s and s <= want and (newest is None or s > newest):
                    newest = s

    if newest is None:
        return [Check("rate_curve", FAIL,
                      f"no {RATE_DATASET} row at or before {want}", {})]

    # Trading days, not calendar days: a Monday session is 1 trading day after
    # Friday, and counting calendar days would flag every weekend.
    gap = 0
    cursor = d
    curve_date = date.fromisoformat(newest)
    while cursor > curve_date and gap <= RATE_CURVE_FAIL_TRADING_DAYS + 1:
        cursor = market_gate.previous_trading_day(cursor, data_root=settings.data_root)
        gap += 1

    data = {"curve_date": newest, "trading_days_stale": gap}
    detail = (f"newest curve {newest}, {gap} trading day"
              f"{'' if gap == 1 else 's'} before {want}")
    if gap > RATE_CURVE_FAIL_TRADING_DAYS:
        return [Check("rate_curve", FAIL,
                      detail + " -- rates_sync has stopped landing; everything "
                      "priced since is discounting off a frozen curve", data)]
    if gap > RATE_CURVE_WARN_TRADING_DAYS:
        return [Check("rate_curve", WARN,
                      detail + f" -- expected {RATE_CURVE_WARN_TRADING_DAYS} "
                      "at the vendor's steady T-2; a run probably did not land",
                      data)]
    return [Check("rate_curve", PASS, detail, data)]


def run_checks(settings: Settings, d: date, logger: JsonlLogger) -> list[Check]:
    """Every check for one trading day."""
    checks: list[Check] = []
    checks += check_snapshots(settings, d)
    checks += check_flatfiles(settings, d)
    checks += check_partitions(settings, d)
    checks += check_vol_surface(settings, d)
    checks += check_underlying_coverage(settings, d)
    checks += check_underlying_window(settings, d)
    checks += check_rate_curve(settings, d)
    checks += check_websocket(settings, d, logger)
    checks += check_disk(settings, d)
    return checks


def _render(d: date, checks: list[Check]) -> str:
    """PASS/FAIL table, in the style of ``ingest.entitlements``."""
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


def last_completed_session(
    data_root: Path | str | None = None, now: datetime | None = None
) -> date:
    """The newest session this audit can fairly grade: T-1, but not too early.

    A session is not gradeable the moment it ends. Its data is produced the
    *following* day by a chain that finishes with the surface job at 12:15 ET
    (``deploy/schedule.json``), and the audit's own 12:30 slot sits right
    after it. So the newest gradeable session is the newest trading day whose
    following day has already passed 12:15 ET.

    Plain T-1 was right for the 12:30 run and for the 18:17 ET scheduled CI
    run, and wrong for everything earlier: a push at 11:55 ET on 2026-09-22
    failed the ``box`` workflow on ``vol_surface`` for 2026-09-21, a partition
    that was twenty minutes from being written. The audit was reporting a hole
    where there was only a job that had not come due.

    Stepping back by the *processing* day rather than a fixed count keeps
    Monday honest: Friday's session is processed by Saturday's run, so on
    Monday morning Friday is already complete and stays the target.

    This only moves the date the audit *defaults* to. It never softens a
    check: once a session is in scope it is graded exactly as strictly as
    before, and a surface job that genuinely fails to run still fails the
    12:30 audit.
    """
    now = now or market_gate.now_et()
    d = market_gate.previous_trading_day(now.date(), data_root)
    while datetime.combine(
        d + timedelta(days=1), PIPELINE_DONE_ET, tzinfo=now.tzinfo
    ) > now:
        d = market_gate.previous_trading_day(d, data_root)
    return d


def main(argv: list[str] | None = None) -> None:
    """Entry point; defaults --date to the last completed session, then run_job.

    The date must be resolved before ``run_job``: the audit runs Tue-Sat to
    grade the prior session, and ``run_job``'s market gate would otherwise
    exit 0 on the Saturday run.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    # argparse also accepts ``--date=X``; a bare "--date" membership test
    # misses that form, and the appended default would silently override the
    # date the caller asked to audit. The default is computed against the
    # configured root: main() runs before run_job's Settings.load(), so a
    # DATA_ROOT that lives only in .env needs config.default_data_root.
    if not any(a == "--date" or a.startswith("--date=") for a in argv):
        target = last_completed_session(default_data_root())
        argv += ["--date", target.isoformat()]
    run_job(JOB, _main_fn, argv)


if __name__ == "__main__":
    main()
