"""Create/update one Healthchecks.io check per ingestion job.

Why per-job, and why schedules: a single shared check reports green as soon as
*any* job pings it, so the failure this repo actually suffers — a job that
quietly stops running — is invisible. Giving each job its own check with the
cron expression it is actually scheduled on means Healthchecks alerts on a
*missing* run, not only on a crashing one.

Needs a **management API key** (Healthchecks project -> Settings -> API Access
-> "API key (full access)"), which is different from the ping key the jobs use.
Self-hosted instances pass ``--api-base https://<host>/api/v3``; note the ping
root is separate and lives in ``HEALTHCHECKS_BASE`` (``https://<host>/ping``).

    python scripts/setup_healthchecks.py --api-key hcak_xxx [--dry-run]

Idempotent: re-running updates existing checks in place (matched by slug).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_API_BASE = "https://healthchecks.io/api/v3"

# job -> (cron schedule, grace_minutes, description)
#
# Schedules mirror deploy/crontab. Where a job runs on several cron lines, the
# expression below is the one that must hold during the trading session; the
# grace absorbs the rest. Timezone is set per check to America/New_York.
JOBS: dict[str, tuple[str, int, str]] = {
    "contracts_sync":   ("0 8 * * 1-5",     90,  "SPY/SPX contract universe (also runs 16:30)"),
    "dividends_sync":   ("0 8 * * 1-5",     90,  "SPY dividends and splits"),
    "holidays_sync":    ("0 7 * * 0",       240, "Market calendar refresh (weekly)"),
    "snapshot_sweep":   ("* 10-15 * * 1-5", 10,  "1/min full-chain snapshots - THE irreplaceable dataset"),
    "ws_minute_bars":   ("25 9 * * 1-5",    600, "Delayed options websocket capture (long-running)"),
    "trades_watchlist": ("*/5 10-15 * * 1-5", 20, "Same-day option trades for the liquid watchlist"),
    "underlying_bars":  ("5 8 * * 2-6",     90,  "SPY minute bars, T-1 only"),
    "grouped_daily":    ("10 8 * * 2-6",    90,  "Whole-market daily bars (SPY cross-check)"),
    "flatfile_pull":    ("5 11 * * 2-6",    120, "T-1 S3 flat files - the authoritative record"),
    "reconcile":        ("30 11 * * 2-6",   120, "Rewrite minute bars from the flat file"),
    "coverage_audit":   ("30 12 * * 2-6",   180, "Did yesterday actually land? Fails loudly if not"),
}

SLUG_PREFIX = "massive-"
TZ = "America/New_York"


def slug_for(job: str) -> str:
    """Must match ``ingest.common.cli.healthcheck_slug``."""
    return SLUG_PREFIX + job.strip().lower().replace("_", "-")


def _request(
    method: str, path: str, api_key: str, payload: dict | None = None,
    api_base: str = DEFAULT_API_BASE,
) -> object:
    url = f"{api_base.rstrip('/')}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", api_key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(
            f"ERROR: {method} {path} -> HTTP {exc.code}: {detail}\n"
            "Check that this is a full-access management API key "
            "(Settings -> API Access), not the ping key."
        ) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"ERROR: cannot reach {api_base}: {exc.reason}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="setup_healthchecks.py")
    parser.add_argument("--api-key", required=True, help="management API key (hcak_...)")
    parser.add_argument("--dry-run", action="store_true", help="print, do not write")
    parser.add_argument(
        "--api-base", default=DEFAULT_API_BASE,
        help="management API root; self-hosted is https://<host>/api/v3 "
             f"(default: {DEFAULT_API_BASE})",
    )
    args = parser.parse_args(argv)

    existing_body = _request("GET", "/checks/", args.api_key, api_base=args.api_base)
    existing = {
        c.get("slug"): c
        for c in (existing_body or {}).get("checks", [])  # type: ignore[union-attr]
    }
    print(f"{len(existing)} existing check(s) in this project\n")

    for job, (schedule, grace, desc) in JOBS.items():
        slug = slug_for(job)
        payload = {
            "name": f"massive {job}",
            "slug": slug,
            "schedule": schedule,
            "tz": TZ,
            "grace": grace * 60,
            "desc": desc,
            "channels": "*",          # notify via every configured integration
            "unique": ["slug"],       # idempotent create-or-update
        }
        action = "update" if slug in existing else "create"
        print(f"  {action:<6} {slug:<26} {schedule:<20} grace={grace}m")
        if not args.dry_run:
            _request("POST", "/checks/", args.api_key, payload, api_base=args.api_base)

    if args.dry_run:
        print("\n(dry run - nothing written)")
        return 0

    print(
        "\nDone. Next:\n"
        "  1. Copy the project's PING KEY (Settings -> Ping Key) into .env as\n"
        "     HEALTHCHECKS_PING_KEY=...\n"
        "  2. Add a notification channel (email/Slack) in the Healthchecks UI,\n"
        "     or none of this will actually reach you.\n"
        "  3. Verify:  venv/bin/python -m ingest.jobs.coverage_audit --date <a trading day>\n"
        "     then confirm massive-coverage-audit shows a ping."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
