"""history_audit: is every trading day in the archive actually present?

``coverage_audit`` answers "did today work". Nothing answered "is the history
complete", and the difference is not academic: 19 trading days between
2026-02-15 and 2026-04-04 sat missing from the archive while every daily
check passed, because the daily check only ever looks at T-1. They surfaced
only when someone went looking by hand.

The hard part is not finding dates with no data -- it is deciding whether a
missing date is a market holiday or a hole. ``_meta/holidays.json`` cannot
answer that: ``holidays_sync`` reads ``/v1/marketstatus/upcoming``, so it
holds the next ten months and knows nothing about 2023. Guessing from a
hardcoded holiday list would be worse, because it silently misses ad-hoc
closures (a national day of mourning is not on anybody's rrule).

So the vendor decides. They publish a flat file for every session and none
for a closed day, which makes a HEAD on the trades object an authoritative
trading-day oracle. Only dates with nothing on disk are probed, so a healthy
archive costs no network at all, and every answer is cached in
``_meta/trading_days.json`` -- which accumulates into the verified historical
calendar the repo otherwise does not have.

Run: ``python -m ingest.jobs.history_audit [--start YYYY-MM-DD]
[--end YYYY-MM-DD] [--offline]``

``run_job`` gates on the *run* date, not the audited range, so add ``--force``
to audit from a weekend.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ingest.common import landing, market_gate
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger

JOB = "history_audit"
COVERAGE_NAME = "history_coverage.json"
CALENDAR_NAME = "trading_days.json"

# Clean datasets flatfile_pull is responsible for, keyed by the flat-file
# dataset that produces them. Every one of these must be present on a session
# day; a date carrying some but not all of them is a partial write, which is
# worse than a clean absence because a whole-partition read still returns rows.
CLEAN_DATASETS = ("option_trades", "option_minute_bars", "option_day_bars")

# The vendor object that decides whether a date was a session.
ORACLE_DATASET = "trades_v1"

OK, GAP, PARTIAL, HOLIDAY, UNKNOWN = "OK", "GAP", "PARTIAL", "HOLIDAY", "UNKNOWN"

# Verdicts that mean data we should have is not there.
BAD = (GAP, PARTIAL)


@dataclass
class DayStatus:
    """What the archive holds for one candidate session day."""

    day: date
    verdict: str
    present: dict[str, bool] = field(default_factory=dict)

    @property
    def missing(self) -> list[str]:
        return [ds for ds, ok in sorted(self.present.items()) if not ok]


def _partition_has_rows(settings: Settings, dataset: str, d: date) -> bool:
    """True when the clean partition holds at least one non-empty parquet.

    Presence of the directory is not enough: ``--replace`` moves the previous
    file aside before the new one lands, so an interrupted re-filter can leave
    a partition that exists and contains nothing.
    """
    part = Path(settings.data_root) / "clean" / dataset / f"dt={d.isoformat()}"
    if not part.is_dir():
        return False
    try:
        import pyarrow.parquet as pq
    except ImportError:  # pragma: no cover - only on pyarrow-less hosts
        return any(part.glob("*.parquet"))
    for path in part.glob("*.parquet"):
        try:
            if pq.ParquetFile(path).metadata.num_rows > 0:
                return True
        except Exception:  # noqa: BLE001 - an unreadable file is not presence
            continue
    return False


def load_calendar(data_root: Path | str | None = None) -> dict[str, bool]:
    """Cached ``{date: was_a_session}`` answers from previous audits."""
    path = landing.meta_path(CALENDAR_NAME, data_root=data_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: bool(v) for k, v in data.items()} if isinstance(data, dict) else {}


def save_calendar(calendar: dict[str, bool], data_root: Path | str | None = None) -> Path:
    """Persist the verified trading-day calendar, newest answers included."""
    path = landing.meta_path(CALENDAR_NAME, data_root=data_root)
    path.write_text(json.dumps(dict(sorted(calendar.items())), indent=2) + "\n",
                    encoding="utf-8")
    return path


def candidate_days(start: date, end: date) -> list[date]:
    """Weekdays in ``[start, end]`` -- the only dates that can be sessions."""
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _vendor_has_session(s3: Any, bucket: str, d: date) -> bool | None:
    """True/False from the vendor, or None when the answer is unavailable.

    A 404 is a closed market. Anything else -- a 403 on an unentitled window,
    a network failure -- is explicitly *not* evidence of a holiday, so it
    returns None and the day is reported UNKNOWN rather than quietly excused.
    """
    from botocore.exceptions import ClientError

    from ingest.jobs.flatfile_pull import s3_key

    try:
        s3.head_object(Bucket=bucket, Key=s3_key(ORACLE_DATASET, d))
        return True
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return False if status == 404 else None
    except Exception:  # noqa: BLE001 - unreachable vendor is not a holiday
        return None


def audit_range(
    settings: Settings,
    start: date,
    end: date,
    logger: JsonlLogger | None = None,
    offline: bool = False,
    s3_factory: Any = None,
) -> tuple[list[DayStatus], dict[str, bool]]:
    """Classify every weekday in the range; returns (statuses, calendar).

    The vendor is consulted lazily: a date whose data is all present is a
    session by definition and never needs a probe, so a complete archive
    performs no network calls at all.
    """
    calendar = load_calendar(settings.data_root)
    statuses: list[DayStatus] = []
    s3 = None

    for d in candidate_days(start, end):
        present = {ds: _partition_has_rows(settings, ds, d) for ds in CLEAN_DATASETS}
        if all(present.values()):
            calendar[d.isoformat()] = True
            statuses.append(DayStatus(d, OK, present))
            continue
        if any(present.values()):
            # Some datasets landed, so the market was open. No probe needed.
            calendar[d.isoformat()] = True
            statuses.append(DayStatus(d, PARTIAL, present))
            continue

        known = calendar.get(d.isoformat())
        if known is None and not offline:
            if s3 is None:
                from ingest.common.s3 import s3_client
                s3 = s3_client(settings) if s3_factory is None else s3_factory()
            known = _vendor_has_session(s3, settings.massive_s3_bucket, d)
            if known is not None:
                calendar[d.isoformat()] = known
            if logger is not None:
                logger.log("history_probe", date=d.isoformat(), session=known)

        if known is True:
            statuses.append(DayStatus(d, GAP, present))
        elif known is False:
            statuses.append(DayStatus(d, HOLIDAY, present))
        else:
            statuses.append(DayStatus(d, UNKNOWN, present))

    return statuses, calendar


def _render(start: date, end: date, statuses: list[DayStatus]) -> str:
    """Summary counts, then every date that needs attention."""
    counts: dict[str, int] = {}
    for s in statuses:
        counts[s.verdict] = counts.get(s.verdict, 0) + 1
    lines = [
        f"history_audit -- {start.isoformat()} .. {end.isoformat()}",
        "-" * 78,
        "  ".join(f"{k}={v}" for k, v in sorted(counts.items())),
    ]
    problems = [s for s in statuses if s.verdict in BAD or s.verdict == UNKNOWN]
    if problems:
        lines.append("-" * 78)
        for s in problems:
            detail = ",".join(s.missing) if s.verdict == PARTIAL else "no data"
            lines.append(f"{s.verdict:<8} {s.day.isoformat()}  {detail}")
    lines.append("-" * 78)
    return "\n".join(lines)


class HistoryGapError(RuntimeError):
    """Raised when a session day is missing, so run_job exits 1 and pings /fail."""


def _default_start(settings: Settings) -> date:
    """Earliest date the archive has raw flat files for."""
    raw = Path(settings.data_root) / "raw" / "flatfiles" / ORACLE_DATASET
    days = sorted(p.name[3:] for p in raw.glob("dt=*") if p.is_dir())
    return date.fromisoformat(days[0]) if days else market_gate.today_et()


def _main_fn(args, settings: Settings, logger: JsonlLogger):
    start = date.fromisoformat(args.start) if getattr(args, "start", None) else \
        _default_start(settings)
    end = date.fromisoformat(args.end) if getattr(args, "end", None) else \
        market_gate.previous_trading_day(market_gate.today_et(), settings.data_root)

    statuses, calendar = audit_range(
        settings, start, end, logger, offline=getattr(args, "offline", False)
    )

    for s in statuses:
        if s.verdict != OK:
            logger.log("history_day", date=s.day.isoformat(), verdict=s.verdict,
                       missing=s.missing)
    print(_render(start, end, statuses), file=sys.stderr)

    payload = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "generated_at": market_gate.now_et().isoformat(),
        "days": [
            {"date": s.day.isoformat(), "verdict": s.verdict, "missing": s.missing}
            for s in statuses if s.verdict != OK
        ],
    }
    if not args.dry_run:
        landing.meta_path(COVERAGE_NAME, data_root=settings.data_root).write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        save_calendar(calendar, settings.data_root)

    bad = [s for s in statuses if s.verdict in BAD]
    unknown = [s for s in statuses if s.verdict == UNKNOWN]
    summary = {
        "rows": len(statuses),
        "sessions": len([s for s in statuses if s.verdict in (OK, *BAD)]),
        "gaps": len(bad),
        "unknown": len(unknown),
    }
    if bad:
        shown = ", ".join(s.day.isoformat() for s in bad[:10])
        more = f" (+{len(bad) - 10} more)" if len(bad) > 10 else ""
        raise HistoryGapError(
            f"{len(bad)} session day(s) missing between {start} and {end}: {shown}{more}"
        )
    return summary


def main(argv: list[str] | None = None) -> int:
    """Entry point; ``--start``/``--end``/``--offline`` are job-specific."""
    argv = list(argv) if argv is not None else sys.argv[1:]
    start = end = None
    offline = False
    rest: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--start" and i + 1 < len(argv):
            start = argv[i + 1]
            i += 2
        elif argv[i] == "--end" and i + 1 < len(argv):
            end = argv[i + 1]
            i += 2
        elif argv[i] == "--offline":
            offline = True
            i += 1
        else:
            rest.append(argv[i])
            i += 1

    # run_job gates on the run date, which for this job is incidental -- the
    # subject is the archive, not today. Defaulting to the previous trading
    # day (the same convention flatfile_pull uses) means the weekly Saturday
    # cron line gates on Friday's session and needs no --force.
    if "--date" not in rest:
        prev = market_gate.previous_trading_day(market_gate.today_et())
        rest = rest + ["--date", prev.isoformat()]

    def main_fn(a, st, log):
        a.start, a.end, a.offline = start, end, offline
        return _main_fn(a, st, log)

    return run_job(JOB, main_fn, rest)  # run_job exits; return is for tests


if __name__ == "__main__":
    sys.exit(main())
