#!/usr/bin/env python
"""Build vol_surface over a range of sessions.

``python -m pricing.surface --date D`` fits one day. This does the archive,
and is the right entry point after a re-filter, after the rates warehouse is
extended, or whenever the fit itself changes -- the output is derived, so
rebuilding it is always safe.

Sessions come from ``_meta/trading_days.json``, the calendar
``ingest.jobs.history_audit`` verifies against the vendor, so this never has
to guess whether a date was a trading day. Run that job first if the file is
absent.

In-process on purpose: the rates curve and the Python interpreter are loaded
once for the whole range instead of once per date, which is the difference
between minutes and an hour.

    venv/bin/python scripts/build_surface.py [--start D] [--end D]
                                             [--root SPXW] [--force]

Existing output for a date is skipped unless ``--force``, so an interrupted
run resumes by re-running the same command. A date whose slices trip an
arbitrage guard is reported and skipped -- one bad session must not end the
run, and the guard's message says which expiries to look at.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.common.config import Settings  # noqa: E402
from ingest.jobs.history_audit import load_calendar  # noqa: E402
from pricing.surface import (  # noqa: E402
    DATASET,
    SURFACE_ROOTS,
    build_for_date,
    rows_from_surfaces,
    write_rows,
)


def already_built(settings: Settings, d: date) -> bool:
    part = Path(settings.data_root) / "clean" / DATASET / f"dt={d.isoformat()}"
    return part.is_dir() and any(part.glob("*.parquet"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_surface")
    parser.add_argument("--start", help="first session, YYYY-MM-DD")
    parser.add_argument("--end", help="last session, YYYY-MM-DD")
    parser.add_argument("--root", action="append", help="OPRA root; repeatable")
    parser.add_argument("--force", action="store_true",
                        help="rebuild dates that already have output")
    args = parser.parse_args(argv)

    settings = Settings.load()
    roots = tuple(args.root) if args.root else SURFACE_ROOTS

    calendar = load_calendar(settings.data_root)
    if not calendar:
        print("FAIL  no _meta/trading_days.json; run "
              "`python -m ingest.jobs.history_audit --force` first", file=sys.stderr)
        return 1

    sessions = sorted(d for d, is_session in calendar.items() if is_session)
    if args.start:
        sessions = [d for d in sessions if d >= args.start]
    if args.end:
        sessions = [d for d in sessions if d <= args.end]

    print(f"[surface] {len(sessions)} sessions, roots={','.join(roots)}", flush=True)
    ok = skipped = empty = failed = 0
    bad: list[str] = []
    t0 = time.time()

    for i, iso in enumerate(sessions, 1):
        d = date.fromisoformat(iso)
        if not args.force and already_built(settings, d):
            skipped += 1
            continue
        try:
            surfaces = build_for_date(settings, d, roots)
        except Exception as exc:  # noqa: BLE001 - one bad date must not end the run
            failed += 1
            bad.append(iso)
            print(f"[surface] {iso} FAILED {type(exc).__name__}: {exc}", file=sys.stderr,
                  flush=True)
            continue
        if not surfaces:
            # No day bars, or no expiry with enough OTM strikes -- reported,
            # not silently counted as success.
            empty += 1
            bad.append(iso)
            print(f"[surface] {iso} EMPTY", file=sys.stderr, flush=True)
            continue
        # write_rows replaces this job's prior output for the date, so a
        # --force rebuild cannot leave two files in the partition and have a
        # whole-partition read return every (date, root, expiry) key twice.
        write_rows(settings, d, rows_from_surfaces(surfaces))
        ok += 1
        if ok % 100 == 0:
            print(f"[surface] ({i}/{len(sessions)}) {iso}  ok={ok} skipped={skipped} "
                  f"empty={empty} failed={failed}  {time.time() - t0:.0f}s", flush=True)

    print(f"[surface] complete: {ok} written, {skipped} already present, {empty} empty, "
          f"{failed} failed in {time.time() - t0:.0f}s", flush=True)
    if bad:
        print(f"[surface] dates needing attention: {' '.join(bad[:20])}"
              + (f" (+{len(bad) - 20} more)" if len(bad) > 20 else ""), file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
