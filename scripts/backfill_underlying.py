#!/usr/bin/env python
"""backfill_underlying.py START END — underlying history, in one process.

Why this exists, and why it is more urgent than ``scripts/backfill.sh``: the
equity aggregate endpoints are entitled only inside a ROLLING ~2-year window.
Probed 2026-09-03: SPY 1-minute aggs return 200 for 2024-09-03 and 403 "Your
plan doesn't include this data timeframe" for 2024-06-03. Every day that
passes, one more session falls off the far edge and is unfetchable forever.
The option flat files have no such limit.

So this runs OLDEST-FIRST, the opposite of ``backfill.sh``. There, an
interrupted run should leave you the recent history a 5-45 day horizon model
needs. Here the recent end is in no danger and the old end is expiring, so the
oldest session is always the most valuable one to secure next.

One process, not a shell loop over ``python -m``: at two module launches per
calendar day, interpreter startup dominated everything and a two-year range
took about three hours instead of minutes.

Resume-safe (existing partitions are skipped), quiet on weekends and holidays,
and tolerant of 403 at the old edge -- that is the expected answer there, not
an error. Healthchecks pings are suppressed, because these jobs own daily
checks and several hundred backfill pings would bury the signal that today's
scheduled run worked.

    venv/bin/python scripts/backfill_underlying.py 2024-09-04 2026-09-02
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Before importing anything that builds Settings: an empty ping key makes
# cli.healthcheck_url return (None, False), so no run here pings anything.
os.environ["HEALTHCHECKS_PING_KEY"] = ""

from ingest.common import market_gate  # noqa: E402
from ingest.common.config import Settings  # noqa: E402
from ingest.common.logging_utils import JsonlLogger  # noqa: E402
from ingest.jobs import grouped_daily, underlying_bars  # noqa: E402

JOBS = (
    ("underlying_minute_bars", underlying_bars, False),
    ("underlying_day_bars", grouped_daily, True),
)


def _have(settings: Settings, dataset: str, d: date) -> bool:
    part = Path(settings.data_root) / "clean" / dataset / f"dt={d.isoformat()}"
    return part.is_dir() and any(part.glob("*.parquet"))


def _args(d: date) -> argparse.Namespace:
    return argparse.Namespace(
        date=d.isoformat(), limit=None, dry_run=False, force=True, underlying=None
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="extra pause between sessions (the shared token "
                         "bucket already paces the requests)")
    a = ap.parse_args(argv)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    if start > end:
        print("start after end", file=sys.stderr)
        return 2

    settings = Settings.load()
    logger = JsonlLogger(path=None, echo=False)
    done = dict.fromkeys((n for n, _, _ in JOBS), 0)
    had = dict.fromkeys((n for n, _, _ in JOBS), 0)
    unfetchable = dict.fromkeys((n for n, _, _ in JOBS), 0)
    failed: list[tuple[str, date, str]] = []

    sessions = []
    d = start
    while d <= end:
        if market_gate.is_trading_day(d, settings.data_root):
            sessions.append(d)
        d += timedelta(days=1)
    print(f"[backfill-underlying] {len(sessions)} sessions {start}..{end}", flush=True)

    t0 = time.time()
    for i, day in enumerate(sessions, 1):
        for dataset, mod, keep_all in JOBS:
            if _have(settings, dataset, day):
                had[dataset] += 1
                continue
            # The client retries 429 itself, but a long backfill still walks
            # into one occasionally; a transient throttle must not cost a
            # session that will be unfetchable in a few months.
            for attempt in range(1, 4):
                try:
                    if keep_all:
                        mod._main_fn(_args(day), settings, logger, False)
                    else:
                        mod._main_fn(_args(day), settings, logger)
                    if _have(settings, dataset, day):
                        done[dataset] += 1
                    break
                except PermissionError:
                    # Past the entitlement window. Expected at the old edge.
                    unfetchable[dataset] += 1
                    break
                except Exception as exc:  # noqa: BLE001 - one bad day must not stop the range
                    if attempt == 3:
                        failed.append((dataset, day, f"{type(exc).__name__}: {exc}"))
                    else:
                        time.sleep(2 * attempt)
        if a.sleep:
            time.sleep(a.sleep)
        if i % 50 == 0 or i == len(sessions):
            rate = i / max(time.time() - t0, 1e-9)
            print(f"[backfill-underlying] {i}/{len(sessions)} {day} "
                  f"({rate:.1f} sessions/s) "
                  + " ".join(f"{k}:+{v}" for k, v in done.items()), flush=True)

    print("\n[backfill-underlying] DONE", flush=True)
    for dataset, _, _ in JOBS:
        print(f"  {dataset:24} written={done[dataset]:4d} "
              f"already_had={had[dataset]:4d} unfetchable={unfetchable[dataset]:4d}",
              flush=True)
    for dataset, day, err in failed[:10]:
        print(f"  FAILED {dataset} {day}: {err}", flush=True)
    if len(failed) > 10:
        print(f"  ... {len(failed) - 10} more failures", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
