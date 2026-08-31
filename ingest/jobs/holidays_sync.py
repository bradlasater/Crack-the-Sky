"""holidays_sync: refresh the market-holiday cache.

Fetches ``/v1/marketstatus/upcoming`` and rewrites
``{DATA_ROOT}/_meta/holidays.json`` in the record shape consumed by
``ingest.common.market_gate`` (JSON array of ``{"date", "exchange", "name",
"status", "early_close"?}`` records). The raw response is also landed.

This job is cron'd on Sundays, so it always runs with ``--force`` injected:
the trading-day gate would otherwise skip every non-weekday run.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ingest.common import landing
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.http_client import MassiveClient
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import run_date_from_args

JOB = "holidays_sync"
MARKETSTATUS_UPCOMING_PATH = "/v1/marketstatus/upcoming"

# Keys forwarded verbatim from the API record into the holidays cache.
_KEPT_KEYS = ("date", "exchange", "name", "status", "early_close", "open", "close")


def _holiday_record(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize one marketstatus record to the market_gate holidays shape."""
    return {k: result[k] for k in _KEPT_KEYS if k in result}


def _main_fn(args, settings: Settings, logger: JsonlLogger):
    run_date = run_date_from_args(args)
    client = MassiveClient(settings)
    body = client.get(MARKETSTATUS_UPCOMING_PATH)
    raw_results = body if isinstance(body, list) else body.get("results") or []
    records = [
        _holiday_record(r) for r in raw_results if isinstance(r, dict) and r.get("date")
    ]
    if not args.dry_run:
        raw_path = landing.write_raw("holidays", run_date, raw_results, job=JOB)
        meta = landing.meta_path("holidays.json")
        meta.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
        logger.log(
            "holidays_synced",
            rows=len(records),
            raw_path=str(raw_path),
            meta_path=str(meta),
        )
    return {"rows": len(records)}


def main(argv: list[str] | None = None) -> None:
    """Entry point: ``python -m ingest.jobs.holidays_sync``.

    Forces the run past the trading-day gate (the job is scheduled on
    Sundays purely to refresh the calendar cache).
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--force" not in argv:
        argv.append("--force")
    run_job(JOB, _main_fn, argv)


if __name__ == "__main__":
    main()
