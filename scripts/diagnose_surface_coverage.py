#!/usr/bin/env python
"""Refit vol_surface sessions in memory, per root, and record why any fail.

Diagnostic for docs/plans/surface-coverage.md. Nothing is written to the
warehouse: each session is refit with ``pricing.surface.build_for_date`` one
root at a time, exactly as the scheduled job would fit it, and the outcome is
appended as one JSON line per (session, root) to ``--out``.

    venv/bin/python scripts/diagnose_surface_coverage.py --missing --out diag.jsonl
    venv/bin/python scripts/diagnose_surface_coverage.py --dates 2026-09-01:2026-09-24 --out sep.jsonl

``--missing`` selects sessions with ``option_day_bars`` but no ``vol_surface``
partition; ``--all`` selects every ``option_day_bars`` session; ``--dates``
takes ``D`` or ``D1:D2``; ``--dates-file`` a list, one date per line. Re-running with the same ``--out`` resumes, skipping
sessions already recorded.

A failing record names the expiry whose fit raised (``expiry``, ``dte``) --
the fit's own messages do not -- and, for a calendar rejection from the
Surface guard, the pair and the log-moneyness it failed at. A passing record
carries the slice count and, when a landed partition exists for the session,
whether the refit reproduces it (``matches_landed``).

The thread pin is set to production's 8 before numpy loads, because the fit's
outcome depends on it (scripts/cronjob.sh has the measurement). Workers run
as separate processes, each at that pin, so ``--jobs 4`` uses 32 threads.
"""

from __future__ import annotations

import os

for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
             "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_var, "8")

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from datetime import date  # noqa: E402
from multiprocessing import Pool  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.common.config import Settings, default_data_root  # noqa: E402
from ingest.jobs import partition_dates  # noqa: E402
from pricing import surface as sf  # noqa: E402

CALENDAR = re.compile(
    r"drops ([\d.e+-]+) at k=(-?[\d.]+) between (\S+) \(T=([\d.]+)\) "
    r"and (\S+) \(T=([\d.]+)\)"
)
PARAMS = ("a", "b", "rho", "m", "sigma")


class _SliceFailure(Exception):
    """Carries the expiry whose fit raised, around the original error."""

    def __init__(self, expiry: str, dte: int, error: Exception) -> None:
        super().__init__(str(error))
        self.expiry, self.dte, self.error = expiry, dte, error


def _named_fit_expiry(real):  # noqa: ANN001, ANN202
    """Wrap ``_fit_expiry`` so a failure says which expiry it was."""
    def wrapper(expiry, dte, *args, **kwargs):  # noqa: ANN001, ANN202
        try:
            return real(expiry, dte, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised with its expiry
            raise _SliceFailure(str(expiry), int(dte), exc) from exc
    return wrapper


# At import, not in main(): workers may be started by forkserver or spawn
# (the Python 3.14 default on Linux), which re-import this module rather than
# inheriting the parent's patched state.
sf._fit_expiry = _named_fit_expiry(sf._fit_expiry)


def _settings() -> Settings:
    return Settings(massive_api_key="unused", data_root=default_data_root())


def _landed(settings: Settings, d: date, root: str) -> list[dict[str, Any]] | None:
    try:
        surf = sf.load_surface(settings, d, root)
    except sf.SurfaceError:
        return None
    return [
        {"expiration_date": s.expiration_date, **{p: getattr(s, p) for p in PARAMS}}
        for s in surf.slices
    ]


def refit(iso: str) -> list[dict[str, Any]]:
    """Refit one session per root; one record per root."""
    settings = _settings()
    d = date.fromisoformat(iso)
    out: list[dict[str, Any]] = []
    for root in sf.SURFACE_ROOTS:
        rec: dict[str, Any] = {"date": iso, "root": root}
        t0 = time.time()
        try:
            surfaces = sf.build_for_date(settings, d, roots=(root,))
        except _SliceFailure as exc:
            rec.update(ok=False, stage="slice", err=type(exc.error).__name__,
                       msg=str(exc.error)[:300], expiry=exc.expiry, dte=exc.dte)
        except Exception as exc:  # noqa: BLE001 - the Surface guard, or anything else
            rec.update(ok=False, stage="surface", err=type(exc).__name__,
                       msg=str(exc)[:300])
            m = CALENDAR.search(str(exc))
            if m:
                rec.update(drop=float(m[1]), k=float(m[2]), expiry=m[3],
                           dte=(date.fromisoformat(m[3]) - d).days, pair=m[5])
        else:
            surf = surfaces.get(root)
            rec["ok"] = surf is not None
            if surf is None:
                rec["err"] = "no chain"
            else:
                rec["slices"] = len(surf)
                landed = _landed(settings, d, root)
                if landed is not None:
                    got = [
                        {"expiration_date": s.expiration_date,
                         **{p: getattr(s, p) for p in PARAMS}}
                        for s in surf.slices
                    ]
                    rec["matches_landed"] = got == landed
        rec["secs"] = round(time.time() - t0, 1)
        out.append(rec)
    return out


def _select(args: argparse.Namespace, settings: Settings) -> list[str]:
    bars = [d.isoformat() for d in partition_dates(settings, "option_day_bars")]
    if args.dates_file:
        wanted = {line.strip() for line in Path(args.dates_file).read_text().splitlines()}
        return [d for d in bars if d in wanted]
    if args.dates:
        lo, _, hi = args.dates.partition(":")
        hi = hi or lo
        return [d for d in bars if lo <= d <= hi]
    if args.missing:
        have = {d.isoformat() for d in partition_dates(settings, sf.DATASET)}
        return [d for d in bars if d not in have]
    return bars


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="diagnose_surface_coverage")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--missing", action="store_true",
                       help="sessions with day bars but no vol_surface")
    which.add_argument("--all", action="store_true", help="every day-bar session")
    which.add_argument("--dates", help="D or D1:D2 (inclusive)")
    which.add_argument("--dates-file", help="file with one YYYY-MM-DD per line")
    parser.add_argument("--out", required=True, help="JSONL output; resumes if present")
    parser.add_argument("--jobs", type=int, default=4, help="worker processes")
    args = parser.parse_args(argv)

    out = Path(args.out)
    done: set[str] = set()
    if out.exists():
        done = {json.loads(line)["date"] for line in out.read_text().splitlines() if line}
    todo = [d for d in _select(args, _settings()) if d not in done]
    print(f"[diagnose] {len(todo)} sessions to refit ({len(done)} already recorded)",
          flush=True)
    t0 = time.time()
    with Pool(args.jobs) as pool, out.open("a") as fh:
        for i, recs in enumerate(pool.imap_unordered(refit, todo), 1):
            for rec in recs:
                fh.write(json.dumps(rec) + "\n")
            fh.flush()
            if i % 20 == 0 or i == len(todo):
                print(f"[diagnose] {i}/{len(todo)} in {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
