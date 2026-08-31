"""Shared argparse runner for all ingestion jobs.

``run_job(job_name, main_fn)`` handles the cross-cutting concerns every job
shares: argument parsing, settings, JSONL run logging, the market-calendar
gate, timing, job_end summary logging, healthcheck pings and exit codes.

``main_fn`` is called as ``main_fn(args, settings, logger)`` and should
return an optional dict of summary fields (e.g. ``{"rows": n, "bytes": b}``)
which are merged into the ``job_end`` event.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from typing import Any, Callable, Mapping

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


def _ping(url: str | None, suffix: str = "") -> None:
    """Ping the healthchecks URL (best effort; never raises)."""
    if not url:
        return
    try:
        requests.get(url + suffix, timeout=10)
    except Exception as exc:  # noqa: BLE001 - healthchecks must not fail jobs
        print(f"warning: healthcheck ping failed: {exc}", file=sys.stderr)


def run_job(job_name: str, main_fn: MainFn, argv: list[str] | None = None) -> None:
    """Run ``main_fn`` with standard job plumbing, then exit 0 (ok) / 1 (error).

    Steps: parse args -> load settings -> open run log -> market gate (unless
    ``--force``) -> time ``main_fn`` -> log ``job_end`` -> healthcheck ping
    (``/fail`` suffix on exception).
    """
    args = build_parser(job_name).parse_args(argv)
    run_date: date = date.fromisoformat(args.date) if args.date else market_gate.today_et()

    settings = Settings.load()
    logger = get_run_logger(job_name, run_date, log_root=settings.log_root)
    start = time.monotonic()
    ping_url = settings.healthchecks_ping_url
    try:
        logger.log(
            "job_start",
            job=job_name,
            date=run_date.isoformat(),
            force=args.force,
            limit=args.limit,
            dry_run=args.dry_run,
            underlying=args.underlying,
        )
        market_gate.require_trading_day(run_date, force=args.force, data_root=settings.data_root)
        summary = main_fn(args, settings, logger) or {}
        duration_s = round(time.monotonic() - start, 3)
        logger.log("job_end", job=job_name, rows=summary.get("rows", 0),
                   bytes=summary.get("bytes", 0), duration_s=duration_s,
                   **{k: v for k, v in summary.items() if k not in ("rows", "bytes")})
        _ping(ping_url)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - top-level job guard
        duration_s = round(time.monotonic() - start, 3)
        logger.log("job_error", job=job_name, error=f"{type(exc).__name__}: {exc}",
                   duration_s=duration_s)
        _ping(ping_url, "/fail")
        logger.close()
        sys.exit(1)
    logger.close()
    sys.exit(0)
