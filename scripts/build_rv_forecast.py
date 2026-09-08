#!/usr/bin/env python
"""Build rv_forecast over the archive of spy_spot sessions.

``python -m signals.har_rv --date D`` forecasts one origin. This does the
whole archive: the series is loaded once and ``signals.har_rv.forecast_rows``
fits every (origin, horizon) in one walk-forward pass -- per-date rebuilds
would re-read a thousand partitions per date, and the output is derived, so
rebuilding it is always safe.

    venv/bin/python scripts/build_rv_forecast.py [--start D] [--end D] [--force]

Existing output for a date is skipped unless ``--force``, so an interrupted
run resumes by re-running the same command. Origins with incomplete history
(fewer than MIN_TRAIN_ROWS complete training rows, or gaps in the feature
window) get no row and are counted, not treated as failures.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

# Must precede the signals import below, and therefore numpy's: OpenBLAS reads
# its thread count once, when the shared library loads. Same reasoning as
# scripts/build_surface.py -- the archive rebuild is where an unreproducible
# fit does the most damage.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
             "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_var, "1")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.common.config import Settings  # noqa: E402
from signals.har_rv import (  # noqa: E402
    DATASET,
    forecast_rows,
    read_series,
    realized_variances,
    write_rows,
)


def already_built(settings: Settings, d: date) -> bool:
    part = Path(settings.data_root) / "clean" / DATASET / f"dt={d.isoformat()}"
    return part.is_dir() and any(part.glob("*.parquet"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_rv_forecast")
    parser.add_argument("--start", help="first origin session, YYYY-MM-DD")
    parser.add_argument("--end", help="last origin session, YYYY-MM-DD")
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild dates that already have output",
    )
    args = parser.parse_args(argv)

    settings = Settings.load()
    rows, calendar = read_series(settings)
    if not rows:
        print("FAIL  no spy_spot partitions; run scripts/build_spy_spot.py first",
              file=sys.stderr)
        return 1

    t0 = time.time()
    series = realized_variances(rows, calendar)
    print(f"[rv_forecast] {len(series)} sessions, fitting walk-forward...", flush=True)
    forecasts = forecast_rows(series)
    print(f"[rv_forecast] {len(forecasts)} rows in {time.time() - t0:.1f}s", flush=True)

    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in forecasts:
        by_date[r["date"]].append(r)

    lo = args.start or ""
    hi = args.end or "9999"
    origins = [d for d in sorted(by_date) if lo <= d <= hi]
    ok = skipped = failed = 0
    bad: list[str] = []
    for i, iso in enumerate(origins, 1):
        d = date.fromisoformat(iso)
        if not args.force and already_built(settings, d):
            skipped += 1
            continue
        try:
            write_rows(settings, d, by_date[iso])
        except Exception as exc:  # noqa: BLE001 - one bad date must not end the range
            failed += 1
            bad.append(f"{iso}: {type(exc).__name__}: {exc}")
            continue
        ok += 1
        if i % 100 == 0 or i == len(origins):
            print(
                f"[rv_forecast] {i}/{len(origins)} {iso} "
                f"(wrote={ok} skip={skipped} fail={failed}) "
                f"{time.time() - t0:.1f}s",
                flush=True,
            )
    short = len(series) - len(by_date)
    print(
        f"[rv_forecast] wrote={ok} skipped={skipped} failed={failed} "
        f"origins_without_forecast={short} in {time.time() - t0:.1f}s",
        flush=True,
    )
    for line in bad[:20]:
        print(f"FAIL  {line}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
