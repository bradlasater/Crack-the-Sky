#!/usr/bin/env python
"""Build spy_spot over a range of sessions and write the calibration report.

``python -m signals.spot --date D`` does one day. This does the archive --
every session that has a SPY day-bar and/or a SPY term-structure row -- and
then writes ``_meta/spy_spot_calibration.json`` over the overlap (sessions
where both exist). That report is what Stage 1.1 reads to decide whether the
parity-only tail needs a Roll-style realised-vol debias.

    venv/bin/python scripts/build_spy_spot.py [--start D] [--end D] [--force]

Existing output for a date is skipped unless ``--force``. Interrupted runs
resume. After the walk, calibration always re-reads every written partition
so the report matches what is on disk.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.common.config import Settings  # noqa: E402
from ingest.jobs import partition_dates, read_partition  # noqa: E402
from ingest.jobs.history_audit import load_calendar  # noqa: E402
from signals.spot import (  # noqa: E402
    DATASET,
    build_for_date,
    calibrate,
    write_calibration,
    write_row,
)


def already_built(settings: Settings, d: date) -> bool:
    part = Path(settings.data_root) / "clean" / DATASET / f"dt={d.isoformat()}"
    return part.is_dir() and any(part.glob("*.parquet"))


def _sessions(settings: Settings, start: str | None, end: str | None) -> list[date]:
    calendar = load_calendar(settings.data_root)
    if calendar:
        sessions = [date.fromisoformat(d) for d, ok in calendar.items() if ok]
    else:
        # Fall back to the union of source partitions when the verified
        # calendar has not been built yet (fresh box, or --offline tests).
        sessions = sorted(
            set(partition_dates(settings, "underlying_day_bars"))
            | set(partition_dates(settings, "atm_term_structure"))
        )
    if start:
        lo = date.fromisoformat(start)
        sessions = [d for d in sessions if d >= lo]
    if end:
        hi = date.fromisoformat(end)
        sessions = [d for d in sessions if d <= hi]
    return sessions


def _read_all(settings: Settings) -> list[dict]:
    rows: list[dict] = []
    for d in partition_dates(settings, DATASET):
        rows.extend(read_partition(settings, DATASET, d))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_spy_spot")
    parser.add_argument("--start", help="first session, YYYY-MM-DD")
    parser.add_argument("--end", help="last session, YYYY-MM-DD")
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild dates that already have output",
    )
    parser.add_argument(
        "--calibrate-only",
        action="store_true",
        help="skip the walk; recompute the overlap report from landed rows",
    )
    args = parser.parse_args(argv)

    settings = Settings.load()
    if not args.calibrate_only:
        sessions = _sessions(settings, args.start, args.end)
        print(f"[spy_spot] {len(sessions)} sessions", flush=True)
        ok = skipped = empty = failed = 0
        bad: list[str] = []
        t0 = time.time()
        for i, d in enumerate(sessions, 1):
            if not args.force and already_built(settings, d):
                skipped += 1
                continue
            try:
                row = build_for_date(settings, d)
            except Exception as exc:  # noqa: BLE001 - one bad day must not end the range
                failed += 1
                bad.append(f"{d}: {type(exc).__name__}: {exc}")
                continue
            if row is None:
                empty += 1
                continue
            write_row(settings, d, row)
            ok += 1
            if i % 50 == 0 or i == len(sessions):
                print(
                    f"[spy_spot] {i}/{len(sessions)} {d} "
                    f"(wrote={ok} skip={skipped} empty={empty} fail={failed}) "
                    f"{time.time() - t0:.1f}s",
                    flush=True,
                )
        print(
            f"[spy_spot] wrote={ok} skipped={skipped} empty={empty} "
            f"failed={failed} in {time.time() - t0:.1f}s",
            flush=True,
        )
        for line in bad[:20]:
            print(f"FAIL  {line}", file=sys.stderr)
        if failed:
            return 1

    rows = _read_all(settings)
    report = calibrate(rows)
    path = write_calibration(settings, report)
    print(
        f"[spy_spot] calibration n_overlap={report['n_overlap']}  "
        f"median_abs={report['median_abs_error']}  "
        f"p90={report['p90_abs_error']}  max={report['max_abs_error']}  "
        f"acf1={report['autocorr_lag1']}  "
        f"error_vs_move={report['error_vs_move']}  "
        f"roll_debias_required={report['roll_debias_required']}",
        flush=True,
    )
    print(f"[spy_spot] wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
