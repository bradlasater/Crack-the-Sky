"""snapshot_sweep: full-chain option snapshots per underlying.

Paginates ``/v3/snapshot/options/{underlying}`` (limit=250) for SPY and
I:SPX, landing clean ``option_snapshots`` parquet (nested payload flattened
via ``schemas.flatten_snapshot``) plus a ``forwards`` record set derived from
put-call parity on the chain.

**This is the only dataset here that cannot be backfilled.** Trades and bars
can be re-pulled from S3 flat files years later; implied volatility, greeks,
open interest and the underlying price exist only at the moment they are
swept. That is why this job runs at the highest cadence and why the other
REST jobs yield API budget to it.

Chains are swept concurrently: SPY (~55 pages, ~7s), I:SPX (~115 pages, ~13s)
and VIX (~7 pages, ~1s) are independent, so wall time is the slowest chain
rather than the sum, which is what makes a 1-minute schedule fit. VIX is
almost free next to the other two. Pagination *within* a chain
stays sequential because ``next_url`` is a chain.

Raw JSONL is off by default (``--raw`` re-enables it): at a 1-minute cadence
it is ~6 GB/day of payload whose every schema-projected field is already in
the parquet.
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import islice

from ingest import schemas
from ingest.common import landing
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.http_client import MassiveClient
from ingest.common.logging_utils import JsonlLogger
from ingest.common.rates import RateCurveError, load_curve
from ingest.jobs import forward_from_parity, parse_underlyings, run_date_from_args, strip_flag

JOB = "snapshot_sweep"
DEFAULT_UNDERLYINGS = ["SPY", "I:SPX", "VIX"]
SNAPSHOT_PATH = "/v3/snapshot/options"


def _sweep_underlying(
    settings: Settings,
    logger: JsonlLogger,
    args,
    underlying: str,
    eod: bool,
    write_raw: bool,
    log_lock: threading.Lock,
) -> dict[str, int]:
    """Snapshot the full chain of one underlying; returns pages/rows.

    Gets its own ``MassiveClient`` (the shared token bucket still bounds the
    process-wide request rate) so page counting stays per-chain when chains
    run concurrently.
    """
    run_date = run_date_from_args(args)
    client = MassiveClient(settings)
    pages = 0
    orig_get = client.get

    def counting_get(path, params=None):  # one GET per snapshot page
        nonlocal pages
        pages += 1
        return orig_get(path, params=params)

    client.get = counting_get  # type: ignore[method-assign]
    try:
        stream = client.paginate(
            f"{SNAPSHOT_PATH}/{underlying}", params={"limit": 250}, limit=250
        )
        if args.limit is not None:
            stream = islice(stream, args.limit)
        raw_results = list(stream)
    finally:
        client.get = orig_get  # type: ignore[method-assign]

    records = [schemas.flatten_snapshot(r) for r in raw_results]

    # Parity gives C - P = e^{-rT}(F - K), so the spread must be undiscounted
    # to get the forward. Without a curve the r=0 approximation is used and
    # the rows say so via method="parity-r0" -- the sweep must never fail
    # because the rates warehouse is behind.
    rate_fn = None
    try:
        curve = load_curve(run_date, data_root=settings.data_root)
        rate_fn = lambda _expiry: curve.at(max((_expiry - run_date).days, 1) / 365.0)  # noqa: E731
    except RateCurveError as exc:
        with log_lock:
            logger.log("forward_rate_unavailable", underlying=underlying, reason=str(exc))
    forwards = forward_from_parity(records, rate_for_expiry=rate_fn, asof_date=run_date)
    label = f"{JOB}-eod" if eod else JOB

    if args.dry_run:
        with log_lock:
            logger.log(
                "snapshot_swept", underlying=underlying, eod=eod, pages=pages,
                rows=len(records), forwards=len(forwards), dry_run=True,
            )
        return {"rows": len(records), "pages": pages, "forwards": len(forwards)}

    raw_path = None
    if write_raw:
        raw_path = landing.write_raw(
            "option_snapshots", run_date, raw_results,
            job=f"{label}-{underlying}", data_root=settings.data_root,
        )
    clean_path = landing.write_clean(
        "option_snapshots", run_date, records,
        job=f"{label}-{underlying}", data_root=settings.data_root,
    )
    forwards_path = None
    if forwards:
        forwards_path = landing.write_clean(
            "forwards", run_date, forwards,
            job=f"{label}-{underlying}", data_root=settings.data_root,
        )

    with log_lock:
        logger.log(
            "snapshot_swept",
            underlying=underlying,
            eod=eod,
            pages=pages,
            rows=len(records),
            forwards=len(forwards),
            atm_forward=round(forwards[0]["forward"], 4) if forwards else None,
            raw_path=str(raw_path) if raw_path else None,
            clean_path=str(clean_path),
            forwards_path=str(forwards_path) if forwards_path else None,
        )
    return {"rows": len(records), "pages": pages, "forwards": len(forwards)}


def _main_fn(args, settings: Settings, logger: JsonlLogger, eod: bool, write_raw: bool):
    underlyings = parse_underlyings(args.underlying, DEFAULT_UNDERLYINGS)
    log_lock = threading.Lock()
    totals = {"rows": 0, "pages": 0, "forwards": 0, "eod": eod}

    def sweep(underlying: str) -> dict[str, int]:
        return _sweep_underlying(
            settings, logger, args, underlying, eod, write_raw, log_lock
        )

    with ThreadPoolExecutor(
        max_workers=max(1, len(underlyings)), thread_name_prefix=JOB
    ) as pool:
        for counters in pool.map(sweep, underlyings):
            totals["rows"] += counters["rows"]
            totals["pages"] += counters["pages"]
            totals["forwards"] += counters["forwards"]
    return totals


def main(argv: list[str] | None = None) -> None:
    """Entry point: ``python -m ingest.jobs.snapshot_sweep [--eod] [--raw]``."""
    argv, eod = strip_flag(list(sys.argv[1:] if argv is None else argv), "--eod")
    argv, write_raw = strip_flag(argv, "--raw")

    def main_fn(a, s, log):
        return _main_fn(a, s, log, eod, write_raw)

    run_job(JOB, main_fn, argv)


if __name__ == "__main__":
    main()
