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
an error.

It pings its own Healthchecks entry (``massive-backfill-underlying``). The
per-day work calls each job's ``_main_fn`` directly rather than through
``cli.run_job``, so the daily ``underlying_bars`` / ``grouped_daily`` checks
are untouched by a few hundred backfill iterations -- but this job still needs
liveness of its own. Relying only on coverage_audit's ``*_window`` checks
would mean a dead backfill stays invisible until a gap drifts within 30 days
of expiry, which is exactly the outcome this whole thing exists to prevent.

    venv/bin/python scripts/backfill_underlying.py 2024-09-04 2026-09-02
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.common.cli import healthcheck_url, ping  # noqa: E402
from ingest.common.config import Settings  # noqa: E402
from ingest.common.http_client import NotEntitledError  # noqa: E402
from ingest.common.logging_utils import JsonlLogger  # noqa: E402
from ingest.jobs import grouped_daily, underlying_bars  # noqa: E402
from ingest.jobs.flatfile_pull import manifest_dates  # noqa: E402

# Attempts per dataset-day before a session is recorded as failed.
ATTEMPTS = 3

JOB = "backfill_underlying"

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
                    help="pause between sessions. The shared token bucket "
                         "paces request *count*, but charges one token per "
                         "call regardless of response size, so a heavy "
                         "endpoint needs pacing the bucket cannot express.")
    ap.add_argument("--max-sessions", type=int, default=None,
                    help="stop after this many sessions that needed work. "
                         "The point of a bounded chunk: these endpoints are "
                         "throttled far harder than /v3/trades, and the API "
                         "budget belongs to option_snapshots first, so the "
                         "backfill is designed to be run nightly and finish "
                         "over days rather than to be pushed through in one "
                         "sitting.")
    ap.add_argument("--datasets", default=",".join(n for n, _, _ in JOBS),
                    help="comma-separated subset to backfill. Split them when "
                         "one endpoint is throttled: grouped_daily returns the "
                         "whole US market per call (12,518 tickers) and 429s "
                         "at a cadence SPY minute aggs sail through.")
    a = ap.parse_args(argv)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    if start > end:
        print("start after end", file=sys.stderr)
        return 2

    wanted = {x.strip() for x in a.datasets.split(",") if x.strip()}
    unknown = wanted - {n for n, _, _ in JOBS}
    if unknown:
        print(f"unknown dataset(s): {sorted(unknown)}", file=sys.stderr)
        return 2
    jobs = [j for j in JOBS if j[0] in wanted]

    settings = Settings.load()
    logger = JsonlLogger(path=None, echo=False)
    ping_url, autocreate = healthcheck_url(settings, JOB)
    ping(ping_url, "/start", autocreate)
    done = dict.fromkeys((n for n, _, _ in jobs), 0)
    had = dict.fromkeys((n for n, _, _ in jobs), 0)
    unfetchable = dict.fromkeys((n for n, _, _ in jobs), 0)
    failed: list[tuple[str, date, str]] = []

    # The vendor's own record of which days it published a tape for.
    # market_gate cannot answer this for the past: holidays.json is fed by
    # /v1/marketstatus/upcoming, so historical holidays are absent and every
    # past weekday looks like a session. Asking for Christmas 2024 burns a
    # request on an endpoint that is throttled hard enough to matter.
    known = {date.fromisoformat(x) for x in manifest_dates(Path(settings.data_root))}
    sessions = sorted(d for d in known if start <= d <= end)
    if not sessions:
        print("[backfill-underlying] no flat-file manifest; nothing to enumerate",
              file=sys.stderr)
        return 2
    print(f"[backfill-underlying] {len(sessions)} sessions {start}..{end}", flush=True)

    t0 = time.time()
    budget_used = 0
    for i, day in enumerate(sessions, 1):
        worked = False
        for dataset, mod, keep_all in jobs:
            if _have(settings, dataset, day):
                had[dataset] += 1
                continue
            worked = True
            # The client retries 429 itself, but a long backfill still walks
            # into one occasionally; a transient throttle must not cost a
            # session that will be unfetchable in a few months.
            for attempt in range(1, ATTEMPTS + 1):
                try:
                    if keep_all:
                        mod._main_fn(_args(day), settings, logger, False)
                    else:
                        mod._main_fn(_args(day), settings, logger)
                except NotEntitledError:
                    # 403: past the entitlement window. Expected at the old
                    # edge, and specifically NOT a bare PermissionError --
                    # EACCES on a parquet write must stay fatal rather than
                    # be filed as "the vendor would not serve this".
                    unfetchable[dataset] += 1
                    break
                except Exception as exc:  # noqa: BLE001 - one bad day must not stop the range
                    if attempt == ATTEMPTS:
                        failed.append((dataset, day, f"{type(exc).__name__}: {exc}"))
                        break
                    time.sleep(2 * attempt)
                    continue

                if _have(settings, dataset, day):
                    done[dataset] += 1
                    break
                # Returned successfully and wrote nothing. Both jobs land no
                # partition for an empty response, so this is indistinguishable
                # here from a session the vendor served as empty -- and
                # silently moving on would leave an expiring session missing
                # while the run still exits 0. Retry, then say so.
                if attempt == ATTEMPTS:
                    failed.append((dataset, day, "no partition written"))
                    break
                time.sleep(2 * attempt)
        if worked:
            budget_used += 1
            if a.sleep:
                time.sleep(a.sleep)
        if a.max_sessions is not None and budget_used >= a.max_sessions:
            print(f"[backfill-underlying] stopping at --max-sessions="
                  f"{a.max_sessions} (reached {day})", flush=True)
            break
        if i % 50 == 0 or i == len(sessions):
            rate = i / max(time.time() - t0, 1e-9)
            print(f"[backfill-underlying] {i}/{len(sessions)} {day} "
                  f"({rate:.1f} sessions/s) "
                  + " ".join(f"{k}:+{v}" for k, v in done.items()), flush=True)

    print("\n[backfill-underlying] DONE", flush=True)
    for dataset, _, _ in jobs:
        print(f"  {dataset:24} written={done[dataset]:4d} "
              f"already_had={had[dataset]:4d} unfetchable={unfetchable[dataset]:4d}",
              flush=True)
    for dataset, day, err in failed[:10]:
        print(f"  FAILED {dataset} {day}: {err}", flush=True)
    if len(failed) > 10:
        print(f"  ... {len(failed) - 10} more failures", flush=True)

    summary = " ".join(f"{k}:+{v}" for k, v in done.items())
    if failed:
        ping(ping_url, "/fail", autocreate,
             body=f"{len(failed)} dataset-days failed; {summary}")
        return 1
    ping(ping_url, "", autocreate, body=summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
