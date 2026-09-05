"""Create/update one Healthchecks.io check per ingestion job.

Why per-job, and why schedules: a single shared check reports green as soon as
*any* job pings it, so the failure this repo actually suffers — a job that
quietly stops running — is invisible. Giving each job its own check with the
cron expression it is actually scheduled on means Healthchecks alerts on a
*missing* run, not only on a crashing one.

Needs a **management API key** (Healthchecks project -> Settings -> API Access
-> "API key (full access)"), which is different from the ping key the jobs use.
Healthchecks has issued these under more than one prefix (``hcak_``, ``hcw_``),
so do not identify a key by its prefix -- the management key is whichever one
the API Access page gives you, and the ping key is the one on the Ping Key page.
Self-hosted instances pass ``--api-base https://<host>/api/v3``; note the ping
root is separate and lives in ``HEALTHCHECKS_BASE`` (``https://<host>/ping``).

    python scripts/setup_healthchecks.py [--dry-run]

With no ``--api-key``, the key is read from ``HEALTHCHECKS_API_KEY`` in the
environment or in ``.env``, so the one-time setup does not need the secret
retyped every time. Prefer this over ``--api-key``: a key on the command line
is not merely in shell history, it is copied verbatim into logs you do not
control. A run of this script over Tailscale SSH put the full key into the
systemd journal, because ``tailscaled`` logs the whole remote command line.
Anything that can read the journal can then read the key.

Idempotent: re-running updates existing checks in place (matched by slug).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_API_BASE = "https://healthchecks.io/api/v3"
API_KEY_ENV = "HEALTHCHECKS_API_KEY"

# job -> (cron schedule, grace_minutes, description)
#
# The data lives in deploy/schedule.json -- the canonical schedule the systemd
# timers are also generated from -- so monitoring cannot drift from what is
# installed. Where a job runs on several cron lines, the check's schedule is
# the expression that must hold during the trading session; the grace absorbs
# the rest. Timezone is set per check to America/New_York.
#
# Rationale that used to sit next to the values, preserved:
#
# ws_minute_bars (grace 480): covers the gap between the 09:25 start and the
# terminal ping at the ~16:35 capture end (market_gate.option_capture_end_et),
# plus ~50 minutes of margin. It was 600 (10h), which pushed detection of a
# missing terminal ping to 19:25; 480 brings it to 17:25 the same day.
#
# ws_minute_bars_alive (extra check, no schedule line of its own): the run
# check above can only answer "did today's capture finish", so a process that
# dies mid-session stays green until that terminal ping is overdue. This second
# check is pinged from the job's 5-minute stats tick.
#
# On coverage, precisely: capture runs 09:25 -> ~16:35 and the first stats tick
# lands ~09:30, but one cron expression cannot say "every 5 minutes from 09:30
# to 16:35" -- the minute field applies to every hour in the hour field.
# Expecting pings outside the capture window would alarm every single day, so
# the schedule is the widest band that sits entirely inside it. That gives
# ~10-minute detection between 10:00 and 15:55; a death in the first 35 minutes
# surfaces at 10:10, and one after 15:55 is left to the run check above at
# 17:25. Covering those two tails exactly would take a second and third check
# for ~55 minutes of the session, which is not worth the alert surface.
#
# backfill_underlying: chips away at the rolling ~2-year equity-aggregate
# window. Without its own check a dead backfill would stay invisible until
# coverage_audit's *_window check escalated a gap to FAIL at the 30-day edge --
# far too late for data that cannot be re-fetched afterwards.
SCHEDULE_JSON = Path(__file__).resolve().parents[1] / "deploy" / "schedule.json"


def load_jobs(path: Path = SCHEDULE_JSON) -> dict[str, tuple[str, int, str]]:
    """job -> (cron schedule, grace_minutes, description), from deploy/schedule.json."""
    data = json.loads(path.read_text(encoding="utf-8"))
    jobs: dict[str, tuple[str, int, str]] = {}
    for unit in data["units"]:
        check = unit.get("healthchecks")
        if check is not None:
            jobs[unit["job"]] = (check["schedule"], check["grace_min"], check["desc"])
    for name, check in data["extra_checks"].items():
        jobs[name] = (check["schedule"], check["grace_min"], check["desc"])
    return jobs


JOBS: dict[str, tuple[str, int, str]] = load_jobs()


SLUG_PREFIX = "massive-"
TZ = "America/New_York"


def api_key_from_env(env_path: Path | None = None) -> str | None:
    """Management key from the process environment, else from ``.env``.

    Deliberately a tiny local parser rather than ``ingest.common.config``:
    ``scripts/`` is not a package and nothing in it imports ``ingest``, and
    ``Settings.load`` would also demand unrelated variables like
    ``MASSIVE_API_KEY`` that this script has no use for.
    """
    key = os.environ.get(API_KEY_ENV)
    if key and key.strip():
        return key.strip()
    path = env_path if env_path is not None else Path(__file__).resolve().parents[1] / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == API_KEY_ENV:
            return value.strip().strip('"').strip("'") or None
    return None


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
    parser.add_argument(
        "--api-key",
        help=f"management API key; defaults to {API_KEY_ENV} in the environment or .env",
    )
    parser.add_argument("--dry-run", action="store_true", help="print, do not write")
    parser.add_argument(
        "--api-base", default=DEFAULT_API_BASE,
        help="management API root; self-hosted is https://<host>/api/v3 "
             f"(default: {DEFAULT_API_BASE})",
    )
    args = parser.parse_args(argv)

    api_key = args.api_key or api_key_from_env()
    if not api_key:
        parser.error(
            "no management API key. Pass --api-key, or set "
            f"{API_KEY_ENV} in the environment or in .env "
            "(Healthchecks project -> Settings -> API Access -> full access). "
            "This is NOT the ping key the jobs ping with."
        )

    existing_body = _request("GET", "/checks/", api_key, api_base=args.api_base)
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
            _request("POST", "/checks/", api_key, payload, api_base=args.api_base)

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
