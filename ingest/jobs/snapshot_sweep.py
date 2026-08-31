"""snapshot_sweep: full-chain option snapshots per underlying.

Paginates ``/v3/snapshot/options/{underlying}`` (limit=250) for SPY and
I:SPX, lands raw JSONL plus clean ``option_snapshots`` parquet (nested
payload flattened via ``schemas.flatten_snapshot``; greeks columns stay
nullable). ``--eod`` tags the run in the logs and output filenames.
"""

from __future__ import annotations

import sys
from itertools import islice

from ingest import schemas
from ingest.common import landing
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.http_client import MassiveClient
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import parse_underlyings, run_date_from_args, strip_flag

JOB = "snapshot_sweep"
DEFAULT_UNDERLYINGS = ["SPY", "I:SPX"]
SNAPSHOT_PATH = "/v3/snapshot/options"


def _sweep_underlying(
    client: MassiveClient,
    settings: Settings,
    logger: JsonlLogger,
    args,
    underlying: str,
    eod: bool,
) -> dict[str, int]:
    """Snapshot the full chain of one underlying; returns pages/rows."""
    run_date = run_date_from_args(args)
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

    label = f"{JOB}-eod" if eod else JOB
    if not args.dry_run:
        raw_path = landing.write_raw(
            "option_snapshots", run_date, raw_results, job=f"{label}-{underlying}"
        )
        clean_path = landing.write_clean(
            "option_snapshots", run_date, records, job=f"{label}-{underlying}"
        )
        logger.log(
            "snapshot_swept",
            underlying=underlying,
            eod=eod,
            pages=pages,
            rows=len(records),
            raw_path=str(raw_path),
            clean_path=str(clean_path),
        )
    else:
        logger.log(
            "snapshot_swept", underlying=underlying, eod=eod,
            pages=pages, rows=len(records), dry_run=True,
        )
    return {"rows": len(records), "pages": pages}


def _main_fn(args, settings: Settings, logger: JsonlLogger, eod: bool):
    client = MassiveClient(settings)
    underlyings = parse_underlyings(args.underlying, DEFAULT_UNDERLYINGS)
    totals = {"rows": 0, "pages": 0, "eod": eod}
    for underlying in underlyings:
        counters = _sweep_underlying(client, settings, logger, args, underlying, eod)
        totals["rows"] += counters["rows"]
        totals["pages"] += counters["pages"]
    return totals


def main(argv: list[str] | None = None) -> None:
    """Entry point: ``python -m ingest.jobs.snapshot_sweep [--eod]``."""
    argv, eod = strip_flag(list(sys.argv[1:] if argv is None else argv), "--eod")

    def main_fn(a, s, log):
        return _main_fn(a, s, log, eod)

    run_job(JOB, main_fn, argv)


if __name__ == "__main__":
    main()
