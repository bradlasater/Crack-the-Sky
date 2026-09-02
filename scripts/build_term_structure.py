#!/usr/bin/env python
"""Build atm_term_structure over a range of sessions.

``python -m pricing.term_structure --date D`` does one day. This does the
archive, and is the right entry point after a re-filter, after the rates
warehouse is extended, or whenever the reduction itself changes -- the output
is derived, so rebuilding it is always safe.

Sessions come from ``_meta/trading_days.json``, the calendar
``ingest.jobs.history_audit`` verifies against the vendor, so this never has
to guess whether a date was a trading day. Run that job first if the file is
absent.

In-process on purpose: the rates curve and the Python interpreter are loaded
once for the whole range instead of once per date, which is the difference
between minutes and an hour.

    venv/bin/python scripts/build_term_structure.py [--start D] [--end D]
                                                    [--root SPXW] [--force]

Existing output for a date is skipped unless ``--force``, so an interrupted
run resumes by re-running the same command.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.common.config import Settings  # noqa: E402
from ingest.jobs import OPTION_ROOTS  # noqa: E402
from ingest.jobs.history_audit import load_calendar  # noqa: E402
from pricing.term_structure import DATASET, build_for_date, write_rows  # noqa: E402


def already_built(settings: Settings, d: date) -> bool:
    part = Path(settings.data_root) / "clean" / DATASET / f"dt={d.isoformat()}"
    return part.is_dir() and any(part.glob("*.parquet"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_term_structure")
    parser.add_argument("--start", help="first session, YYYY-MM-DD")
    parser.add_argument("--end", help="last session, YYYY-MM-DD")
    parser.add_argument("--root", action="append", help="OPRA root; repeatable")
    parser.add_argument("--force", action="store_true",
                        help="rebuild dates that already have output")
    args = parser.parse_args(argv)

    settings = Settings.load()
    roots = tuple(args.root) if args.root else OPTION_ROOTS

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

    print(f"[ts] {len(sessions)} sessions, roots={','.join(roots)}", flush=True)
    ok = skipped = empty = failed = 0
    bad: list[str] = []
    t0 = time.time()

    for i, iso in enumerate(sessions, 1):
        d = date.fromisoformat(iso)
        if not args.force and already_built(settings, d):
            skipped += 1
            continue
        try:
            rows = build_for_date(settings, d, roots)
        except Exception as exc:  # noqa: BLE001 - one bad date must not end the run
            failed += 1
            bad.append(iso)
            print(f"[ts] {iso} FAILED {type(exc).__name__}: {exc}", file=sys.stderr,
                  flush=True)
            continue
        if not rows:
            # No day bars, or no expiry quoting both legs -- reported, not
            # silently counted as success.
            empty += 1
            bad.append(iso)
            print(f"[ts] {iso} EMPTY", file=sys.stderr, flush=True)
            continue
        # write_rows replaces this job's prior output for the date, so a
        # --force rebuild cannot leave two files in the partition and have a
        # whole-partition read return every (date, root, expiry) key twice.
        write_rows(settings, d, rows)
        ok += 1
        if ok % 100 == 0:
            print(f"[ts] ({i}/{len(sessions)}) {iso}  ok={ok} skipped={skipped} "
                  f"empty={empty} failed={failed}  {time.time() - t0:.0f}s", flush=True)

    print(f"[ts] complete: {ok} written, {skipped} already present, {empty} empty, "
          f"{failed} failed in {time.time() - t0:.0f}s", flush=True)
    if bad:
        print(f"[ts] dates needing attention: {' '.join(bad[:20])}"
              + (f" (+{len(bad) - 20} more)" if len(bad) > 20 else ""), file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
