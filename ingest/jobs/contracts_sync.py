"""contracts_sync: sync the option-contract reference universe per underlying.

Paginates ``/v3/reference/options/contracts`` for each underlying (default
SPY,SPX — the SPXW endpoint returns 0 rows because SPXW weeklies are listed
under SPX; SPXW stays accepted when passed explicitly), lands raw JSONL plus
a clean ``contracts`` parquet partition,
and diffs the new contract set against the previous clean partition for the
same underlying, logging ``{event: "contracts_diff", new: n, gone: n}``.

``--expired`` adds an ``expired=true`` pass written to the separate
``contracts_expired`` dataset (same schema), and injects ``--force`` --
see :func:`main` for why that is not optional.
"""

from __future__ import annotations

import sys
from itertools import islice
from typing import Any

from ingest import schemas
from ingest.common import landing, market_gate, ratelimit
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.http_client import MassiveClient
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import (
    latest_clean_records,
    parse_underlyings,
    partition_dates,
    run_date_from_args,
    strip_flag,
)

JOB = "contracts_sync"
# SPXW is deliberately NOT in the default: its endpoint returns 0 rows
# (verified live) because SPXW weeklies are listed under the SPX underlying.
DEFAULT_UNDERLYINGS = ["SPY", "SPX", "VIX"]
CONTRACTS_PATH = "/v3/reference/options/contracts"


def _previous_tickers(
    settings: Settings, dataset: str, run_date, underlying: str
) -> set[str]:
    """Tickers of the most recent partition that actually holds ``underlying``.

    Walking back per underlying is the point. ``latest_clean_records`` returns
    whatever the newest partition is, and the underlyings are synced in order
    into a *shared* partition -- so at the 08:00 run SPY writes today's
    partition first, and SPX and VIX then diff against a partition that does
    not contain them yet. The baseline came back empty and every contract
    looked new: 2026-09-02 reported SPX ``new=28642, gone=0`` on a day the
    universe barely moved, and ``gone`` was structurally always zero, so a
    mass delisting could never have shown up.
    """
    for dt in reversed(partition_dates(settings, dataset)):
        if dt > run_date:
            continue
        tickers = {
            r["ticker"]
            for r in latest_clean_records(settings, dataset, dt)
            if r.get("underlying_ticker") == underlying and r.get("ticker")
        }
        if tickers:
            return tickers
    return set()


def _sync_pass(
    client: MassiveClient,
    settings: Settings,
    logger: JsonlLogger,
    args,
    underlying: str,
    dataset: str,
    expired: bool,
) -> dict[str, int]:
    """Fetch, land and diff contracts for one underlying; returns counters."""
    run_date = run_date_from_args(args)
    params: dict[str, Any] = {
        "underlying_ticker": underlying,
        "order": "asc",
        "sort": "ticker",
    }
    if expired:
        params["expired"] = "true"
    stream = client.paginate(CONTRACTS_PATH, params=params, limit=1000)
    if args.limit is not None:
        stream = islice(stream, args.limit)
    raw_results = list(stream)
    records = [schemas.contract_record(r) for r in raw_results]

    previous = _previous_tickers(settings, dataset, run_date, underlying)
    current = {r["ticker"] for r in records if r.get("ticker")}
    diff = {"new": len(current - previous), "gone": len(previous - current)}

    if not args.dry_run:
        raw_path = landing.write_raw(
            dataset, run_date, raw_results, job=f"{JOB}-{underlying}"
        )
        clean_path = landing.write_clean(
            dataset, run_date, records, job=f"{JOB}-{underlying}"
        )
        logger.log(
            "contracts_synced",
            underlying=underlying,
            dataset=dataset,
            rows=len(records),
            raw_path=str(raw_path),
            clean_path=str(clean_path),
        )
    logger.log("contracts_diff", underlying=underlying, dataset=dataset, **diff)
    return {"rows": len(records), **diff}


def _main_fn(args, settings: Settings, logger: JsonlLogger, expired: bool, user_forced: bool):
    client = MassiveClient(settings, priority=ratelimit.LOW)
    underlyings = parse_underlyings(args.underlying, DEFAULT_UNDERLYINGS)
    totals = {"rows": 0, "new": 0, "gone": 0, "expired_rows": 0}
    # The live-universe pass is per-session data and belongs to sessions only.
    # ``--expired`` injects ``--force`` to clear run_job's gate (see main), and
    # letting that injected force reach this pass would write a `contracts`
    # partition dated a Saturday -- the exact thing main's docstring promises
    # it will not do. So this pass re-checks the calendar itself, and only a
    # force the *caller* typed overrides it. Result: the injected force
    # un-gates the expired pass and nothing else.
    run_date = run_date_from_args(args)
    sync_live = user_forced or market_gate.is_trading_day(run_date, settings.data_root)
    if not sync_live:
        logger.log("contracts_live_pass_skipped", date=run_date.isoformat(),
                   reason="not a trading day; expired pass only")
    for underlying in underlyings:
        if sync_live:
            counters = _sync_pass(client, settings, logger, args, underlying, "contracts", False)
            totals["rows"] += counters["rows"]
            totals["new"] += counters["new"]
            totals["gone"] += counters["gone"]
        if expired:
            counters = _sync_pass(
                client, settings, logger, args, underlying, "contracts_expired", True
            )
            totals["expired_rows"] += counters["rows"]
    return totals


def main(argv: list[str] | None = None) -> None:
    """Entry point: ``python -m ingest.jobs.contracts_sync [--expired]``.

    ``--expired`` injects ``--force``, because otherwise the expired pass can
    never run at all. It is scheduled once a week, on Saturday, and Saturday
    is never a trading day -- so ``run_job``'s market gate raised
    ``SystemExit(0)`` before any work happened. The failure was silent by
    design twice over: the gate exits *quietly* so market holidays do not page,
    and ``run_job`` answers that exit with a Healthchecks *success* ping
    ("exited early (not a trading day, or nothing to do)"), so the check stayed
    green while ``clean/contracts_expired`` was never created. Observed
    2026-09-05: ``job_start`` logged at 09:00:01, process gone one second
    later, no ``job_end``, no partition.

    Injecting it here rather than in ``deploy/crontab`` keeps a hand-run
    ``python -m ingest.jobs.contracts_sync --expired`` on a weekend from
    falling into the same hole, and lets CI assert the invariant.

    The plain weekday runs stay gated on purpose: 08:00 and 16:30 fire Mon-Fri
    regardless of the market calendar, and must not write a contracts
    partition on Thanksgiving. Only the expired pass -- reference data whose
    subject is the universe, not a session -- bypasses the calendar, the same
    way ``holidays_sync`` does.
    """
    argv, expired = strip_flag(list(sys.argv[1:] if argv is None else argv), "--expired")
    user_forced = "--force" in argv
    if expired and not user_forced:
        argv.append("--force")

    def main_fn(a, s, log):
        return _main_fn(a, s, log, expired, user_forced)

    run_job(JOB, main_fn, argv)


if __name__ == "__main__":
    main()
