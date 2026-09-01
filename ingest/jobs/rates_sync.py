"""rates_sync: US Treasury par yield curve and inflation levels.

``/fed/v1/treasury-yields`` returns the full curve (1M through 30Y) back to
1962-01-02, 500 rows per page. ``/fed/v1/inflation`` returns CPI, core CPI,
PCE, core PCE and PCE spending. Both are entitled on this tier.

Why this exists: every option priced in this repo used a hardcoded ``r=0.04``.
For a 5-45 DTE book the correct discount rate is the short end of this curve --
3.84% (1M) to 3.90% (3M) as of 2026-08-28 -- and using 4.00% instead pushes a
small but systematic error into every inverted IV. :mod:`pricing.rates` reads
what this job lands.

Incremental by default (the most recent page, cheap enough to run daily);
``--full`` walks back through the history.

The history walk is **resumable**, because /fed/* enforces a quota that no
polite pacing wins outright -- a full walk gets a few thousand rows further
each run before the endpoint refuses. Progress is kept per dataset in
``_meta/rates_cursor.json`` as the oldest date landed, and the next ``--full``
resumes with ``date.lt=<oldest>`` rather than re-walking from today. Being
rate-limited mid-walk is therefore a normal stopping point (logged
``rates_partial``, exit 0), not a failure; landing nothing at all still fails.

Run: ``python -m ingest.jobs.rates_sync [--full]``   (repeat --full to continue)
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ingest.common import landing
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.http_client import MassiveClient, MassiveHTTPError
from ingest.common.logging_utils import JsonlLogger
from ingest.common.ratelimit import TokenBucket
from ingest.jobs import run_date_from_args, strip_flag

JOB = "rates_sync"
YIELDS_PATH = "/fed/v1/treasury-yields"
INFLATION_PATH = "/fed/v1/inflation"
PAGE = 500
# /fed/* is throttled far harder than the options endpoints. Measured: about 5
# requests, then 429 until roughly a 30s cooldown -- so ~1 request per 6s
# sustained. The default 40 rps bucket blows straight through that and the
# 1/2/4/8/16s retry ladder gives up about a second short of the cooldown.
#
# This is its own budget, not shared with the Polygon jobs. The daily
# incremental run is 2 requests total and is unaffected; only --full pages.
FED_RPS = 0.2
FED_BURST = 5.0

# (dataset, path, schema field names beyond "date")
SOURCES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "treasury_yields",
        YIELDS_PATH,
        ("yield_1_month", "yield_3_month", "yield_6_month", "yield_1_year",
         "yield_2_year", "yield_3_year", "yield_5_year", "yield_7_year",
         "yield_10_year", "yield_20_year", "yield_30_year"),
    ),
    (
        "inflation",
        INFLATION_PATH,
        ("cpi", "cpi_core", "pce", "pce_core", "pce_spending"),
    ),
)


CURSOR_NAME = "rates_cursor.json"


def _load_cursor(settings: Settings) -> dict[str, str]:
    """``{dataset: oldest_date_landed}`` (empty when absent/corrupt)."""
    path = landing.meta_path(CURSOR_NAME, data_root=settings.data_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        # `[]` or `null` parse fine and then blow up on .items(). The cursor is
        # read on every run, including incremental, so a malformed file would
        # break the daily job as well as the backfill.
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _save_cursor(settings: Settings, cursor: dict[str, str]) -> None:
    path = landing.meta_path(CURSOR_NAME, data_root=settings.data_root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cursor, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _record(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Project one API row onto its schema. Missing tenors stay null."""
    out: dict[str, Any] = {"date": str(row.get("date") or "")[:10]}
    for f in fields:
        value = row.get(f)
        out[f] = float(value) if value is not None else None
    return out


def _fetch(
    client: MassiveClient,
    path: str,
    full: bool,
    limit: int | None,
    before: str | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(rows, complete)``, newest-first.

    ``complete`` is False when the walk stopped early because the endpoint
    rate-limited us -- the caller keeps the rows and records a cursor so the
    next run resumes instead of starting over.
    """
    params: dict[str, Any] = {"limit": PAGE, "sort": "date.desc"}
    if before:
        params["date.lt"] = before

    if not full:
        # One page is ~2 years of business days: ample overlap for a daily run,
        # and clean writes are per-partition so re-landing a date is harmless.
        body = client.get(path, params=params)
        return list(body.get("results") or []), True

    rows: list[dict[str, Any]] = []
    complete = True
    try:
        for item in client.paginate(path, params=params, limit=PAGE):
            rows.append(item)
            if limit is not None and len(rows) >= limit:
                break
    except MassiveHTTPError as exc:
        # Only an exhausted quota is a resumable stopping point. A permanent
        # 400/404 -- or exhausted 5xx -- means the endpoint is broken, and
        # labelling that "partial" would bank it as progress and exit 0,
        # hiding the breakage as ordinary rate limiting.
        if exc.status_code != 429:
            raise
        complete = False
    return (rows[:limit] if limit is not None else rows), complete


def _main_fn(args, settings: Settings, logger: JsonlLogger, full: bool):
    run_date = run_date_from_args(args)
    client = MassiveClient(settings, bucket=TokenBucket(rate=FED_RPS, burst=FED_BURST))
    cursor = _load_cursor(settings)
    total = 0
    partial: list[str] = []
    done: list[str] = []

    for dataset, path, fields in SOURCES:
        before = cursor.get(dataset) if full else None
        rows, complete = _fetch(client, path, full, args.limit, before)
        records = [_record(r, fields) for r in rows if r.get("date")]
        if not records:
            logger.log("rates_empty", dataset=dataset, path=path,
                       resumed_before=before, complete=complete)
            if full and before and complete:
                # Walked back to the start of the series on an earlier run.
                # Landing nothing here is completion, not an error.
                done.append(dataset)
                logger.log("rates_history_done", dataset=dataset, oldest=before)
            continue
        dates = [r["date"] for r in records]
        if full:
            oldest = min(dates)
            # Only ever move the cursor further back.
            if dataset not in cursor or oldest < cursor[dataset]:
                cursor[dataset] = oldest
            if not complete:
                partial.append(dataset)
                logger.log("rates_partial", dataset=dataset, oldest=oldest,
                           reason="rate limited; rerun --full to continue")
        if not args.dry_run:
            raw_path = landing.write_raw(
                dataset, run_date, rows, job=JOB, data_root=settings.data_root
            )
            clean_path = landing.write_clean(
                dataset, run_date, records, job=JOB, data_root=settings.data_root
            )
            logger.log(
                "rates_written", dataset=dataset, rows=len(records),
                first=min(dates), last=max(dates),
                raw_path=str(raw_path), clean_path=str(clean_path),
            )
        else:
            logger.log("rates_written", dataset=dataset, rows=len(records),
                       first=min(dates), last=max(dates), dry_run=True)
        total += len(records)

    if full and not args.dry_run:
        _save_cursor(settings, cursor)
        logger.log("rates_cursor_saved", **cursor)

    if total == 0 and not done:
        # Nothing landed and nothing was already complete -- a real failure.
        # (A --full run after the history is fully walked lands 0 rows and is
        # fine; failing there would page every day once the backfill finished.)
        raise RuntimeError(
            "rates_sync landed no rows for any dataset; the endpoints returned "
            "nothing (check entitlement with `python -m ingest.entitlements`)"
        )
    return {
        "rows": total,
        "full": full,
        "partial": ",".join(partial) or None,
        "history_complete": ",".join(done) or None,
    }


def main(argv: list[str] | None = None) -> None:
    """Entry point: ``python -m ingest.jobs.rates_sync [--full]``."""
    argv, full = strip_flag(list(sys.argv[1:] if argv is None else argv), "--full")

    def main_fn(a, s, log):
        return _main_fn(a, s, log, full)

    run_job(JOB, main_fn, argv)


if __name__ == "__main__":
    main()
