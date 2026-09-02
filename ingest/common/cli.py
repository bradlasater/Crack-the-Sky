"""Shared argparse runner for all ingestion jobs.

``run_job(job_name, main_fn)`` handles the cross-cutting concerns every job
shares: argument parsing, settings, JSONL run logging, the market-calendar
gate, timing, job_end summary logging, healthcheck pings and exit codes.

``main_fn`` is called as ``main_fn(args, settings, logger)`` and should
return an optional dict of summary fields (e.g. ``{"rows": n, "bytes": b}``)
which are merged into the ``job_end`` event.

Healthchecks
------------
Monitoring is **per job**. With ``HEALTHCHECKS_PING_KEY`` set, each job pings
``{base}/{key}/massive-{job}`` -- a distinct check per job, auto-created on
first ping via ``?create=1``. That is the point: a single shared check goes
green as soon as *any* job succeeds, so nine dead jobs hide behind one healthy
one, and "this job stopped running" -- the failure mode that actually happens
here -- cannot be detected at all.

Each run sends ``/start`` first, so Healthchecks measures duration and can
alert on a run that hangs rather than only one that crashes. Exceptions send
``/fail`` with the error text as the body.

``HEALTHCHECKS_BASE`` is the **ping root**, not the site root. Hosted is
``https://hc-ping.com`` (the default); a self-hosted instance serves pings
under ``/ping``, so it must be set to ``https://hc.example.internal/ping``.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

import requests

from ingest.common import market_gate
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger, get_run_logger

MainFn = Callable[[argparse.Namespace, Settings, JsonlLogger], Mapping[str, Any] | None]


def build_parser(job_name: str) -> argparse.ArgumentParser:
    """Create the shared argument parser every job uses."""
    parser = argparse.ArgumentParser(prog=f"python -m ingest.jobs.{job_name}")
    parser.add_argument(
        "--date",
        default=None,
        help="trading date YYYY-MM-DD (default: today, ET clock)",
    )
    parser.add_argument("--force", action="store_true", help="run even on closed days")
    parser.add_argument("--limit", type=int, default=None, help="cap items processed (testing)")
    parser.add_argument("--dry-run", action="store_true", help="fetch/parse but write nothing")
    parser.add_argument(
        "--underlying",
        default=None,
        help="comma-separated underlyings (job-specific default when omitted)",
    )
    return parser


HEALTHCHECK_SLUG_PREFIX = "massive-"
PING_TIMEOUT_S = 5  # snapshot_sweep has a 60s budget; never block on monitoring
RETRY_CAP_S = 300.0  # longest single backoff between in-run attempts


def _retry_policy() -> tuple[int, float]:
    """(max attempts, base backoff seconds) for in-run retries.

    Code-only knobs (like ``MASSIVE_MAX_RPS``), not in .env.example:
    ``JOB_MAX_ATTEMPTS`` (default 3) and ``JOB_RETRY_BASE_S`` (default 30).
    """
    attempts = int(os.environ.get("JOB_MAX_ATTEMPTS", "3"))
    base_s = float(os.environ.get("JOB_RETRY_BASE_S", "30"))
    return max(1, attempts), max(0.0, base_s)


def healthcheck_slug(job_name: str) -> str:
    """Healthchecks slug for a job (``contracts_sync`` -> ``massive-contracts-sync``).

    Slugs are lowercase and hyphen-separated; job names use underscores.
    """
    return HEALTHCHECK_SLUG_PREFIX + job_name.strip().lower().replace("_", "-")


def healthcheck_url(settings: Settings, job_name: str) -> tuple[str | None, bool]:
    """Return ``(base_url, autocreate)`` for this job's check.

    ``settings.healthchecks_base`` is the ping root -- ``https://hc-ping.com``
    hosted, ``https://<host>/ping`` self-hosted. Without a ping key there is
    no check to ping and monitoring is silently off.
    """
    if settings.healthchecks_ping_key:
        base = f"{settings.healthchecks_base}/{settings.healthchecks_ping_key}"
        return f"{base}/{healthcheck_slug(job_name)}", True
    return None, False


def ping(
    url: str | None,
    suffix: str = "",
    autocreate: bool = False,
    body: str | None = None,
) -> None:
    """Ping a healthchecks endpoint (best effort; never raises).

    Monitoring must never be able to fail the job it is monitoring, so every
    error here is swallowed to stderr.
    """
    if not url:
        return
    target = url + suffix
    if autocreate:
        target += "?create=1"
    try:
        requests.post(target, data=(body or "")[:10000].encode("utf-8"),
                      timeout=PING_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - healthchecks must not fail jobs
        print(f"warning: healthcheck ping failed: {exc}", file=sys.stderr)


def run_job(job_name: str, main_fn: MainFn, argv: list[str] | None = None) -> None:
    """Run ``main_fn`` with standard job plumbing, then exit 0 (ok) / 1 (error).

    Steps: parse args -> load settings -> open run log -> market gate (unless
    ``--force``) -> time ``main_fn`` -> log ``job_end`` -> healthcheck ping
    (``/fail`` suffix on exception).

    Transient exceptions are retried in-process (see ``_retry_policy``):
    cron has no backoff, so without this a one-off network blip waits a full
    tick -- a full day for the T-1 jobs -- before trying again. Retries share
    one Healthchecks run: a single ``/start`` up front and exactly one
    terminal ping at the end. SystemExit (market gate, explicit exits) and
    KeyboardInterrupt are never retried.
    """
    args = build_parser(job_name).parse_args(argv)
    run_date: date = date.fromisoformat(args.date) if args.date else market_gate.today_et()

    settings = Settings.load()
    logger = get_run_logger(job_name, run_date, log_root=settings.log_root)
    start = time.monotonic()
    ping_url, autocreate = healthcheck_url(settings, job_name)
    ping(ping_url, "/start", autocreate)
    max_attempts, retry_base_s = _retry_policy()
    attempt = 0
    while True:
        attempt += 1
        try:
            logger.log(
                "job_start",
                job=job_name,
                date=run_date.isoformat(),
                force=args.force,
                limit=args.limit,
                dry_run=args.dry_run,
                underlying=args.underlying,
                attempt=attempt,
            )
            market_gate.require_trading_day(run_date, force=args.force,
                                            data_root=settings.data_root)
            summary = main_fn(args, settings, logger) or {}
            duration_s = round(time.monotonic() - start, 3)
            logger.log("job_end", job=job_name, rows=summary.get("rows", 0),
                       bytes=summary.get("bytes", 0), duration_s=duration_s,
                       **{k: v for k, v in summary.items() if k not in ("rows", "bytes")})
            ping(ping_url, "", autocreate,
                 body=f"{job_name} ok: rows={summary.get('rows', 0)} in {duration_s}s")
            break
        except SystemExit as exc:
            # market_gate.require_trading_day() exits 0 on holidays, and cron fires
            # on weekdays regardless of the market calendar. Without a terminal
            # ping here the check stays in "started" and Healthchecks reports a
            # hung run on every market holiday -- a false page roughly ten times a
            # year, which is exactly how alerting gets muted and stops working.
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            duration_s = round(time.monotonic() - start, 3)
            if code == 0:
                ping(ping_url, "", autocreate,
                     body=f"{job_name} exited early (not a trading day, or nothing to do)")
            else:
                ping(ping_url, "/fail", autocreate,
                     body=f"{job_name} exited {code} after {duration_s}s")
            logger.close()
            raise
        except BaseException as exc:  # noqa: BLE001 - must not leave the check hung
            # BaseException, not Exception: KeyboardInterrupt and other
            # BaseExceptions would otherwise skip every handler here and leave the
            # check in "started" until Healthchecks called it a hung run.
            interrupted = isinstance(exc, KeyboardInterrupt)
            duration_s = round(time.monotonic() - start, 3)
            if not interrupted and isinstance(exc, Exception) and attempt < max_attempts:
                sleep_s = min(retry_base_s * (2 ** (attempt - 1)), RETRY_CAP_S)
                logger.log("job_retry", job=job_name, attempt=attempt,
                           error=f"{type(exc).__name__}: {exc}", sleep_s=sleep_s)
                time.sleep(sleep_s)
                continue
            logger.log(
                "job_interrupted" if interrupted else "job_error",
                job=job_name, error=f"{type(exc).__name__}: {exc}", duration_s=duration_s,
            )
            ping(ping_url, "/fail", autocreate,
                 body=f"{job_name} {'interrupted' if interrupted else 'failed'} "
                      f"after {duration_s}s: {type(exc).__name__}: {exc}")
            logger.close()
            if interrupted:
                raise
            sys.exit(1)
    logger.close()
    sys.exit(0)
