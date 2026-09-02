"""Tests for history_audit: the job that makes a *historical* hole loud.

The whole value of this job is telling a market holiday apart from a gap, so
these assert both directions, and assert that the vendor is only consulted
when the archive cannot answer on its own.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from ingest import schemas
from ingest.common import landing
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import history_audit as ha


def _settings(data_root: Path) -> Settings:
    return Settings(
        massive_api_key="test-key",
        massive_s3_bucket="flatfiles",
        data_root=data_root,
        log_root=data_root / "logs",
    )


def _logger() -> JsonlLogger:
    return JsonlLogger(path=None, echo=False)


def _land(root: Path, day: date, datasets=ha.CLEAN_DATASETS) -> None:
    """Write one non-empty parquet per dataset for ``day``."""
    for ds in datasets:
        rows = [{f.name: None for f in schemas.SCHEMAS[ds]}]
        landing.write_clean(ds, day, rows, job="flatfile_pull", data_root=root)


class _S3:
    """Minimal head_object stub; ``sessions`` are the dates that 200."""

    def __init__(self, sessions: set[str]) -> None:
        self.sessions = sessions
        self.calls: list[str] = []

    def head_object(self, Bucket: str, Key: str):  # noqa: N803 - boto3 kwarg
        from botocore.exceptions import ClientError

        day = Key.rsplit("/", 1)[-1].removesuffix(".csv.gz")
        self.calls.append(day)
        if day in self.sessions:
            return {"ContentLength": 1}
        raise ClientError(
            {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadObject",
        )


# ---------------------------------------------------------------------------
# Candidate days
# ---------------------------------------------------------------------------

def test_candidates_are_weekdays_only() -> None:
    days = ha.candidate_days(date(2023, 1, 13), date(2023, 1, 17))
    assert days == [date(2023, 1, 13), date(2023, 1, 16), date(2023, 1, 17)]


# ---------------------------------------------------------------------------
# Holiday vs gap -- the distinction the job exists to make
# ---------------------------------------------------------------------------

def test_vendor_404_is_a_holiday_not_a_gap(tmp_path: Path) -> None:
    s3 = _S3(sessions=set())
    statuses, calendar = ha.audit_range(
        _settings(tmp_path), date(2023, 1, 16), date(2023, 1, 16),
        _logger(), s3_factory=lambda: s3,
    )
    assert [s.verdict for s in statuses] == [ha.HOLIDAY]
    assert calendar["2023-01-16"] is False


def test_vendor_200_with_no_data_is_a_gap(tmp_path: Path) -> None:
    """The failure mode that went unnoticed for years."""
    s3 = _S3(sessions={"2023-02-15"})
    statuses, calendar = ha.audit_range(
        _settings(tmp_path), date(2023, 2, 15), date(2023, 2, 15),
        _logger(), s3_factory=lambda: s3,
    )
    assert [s.verdict for s in statuses] == [ha.GAP]
    assert calendar["2023-02-15"] is True


def test_partial_day_is_a_gap_without_probing(tmp_path: Path) -> None:
    """Some datasets landed, so the market was open -- no oracle needed.

    A partition holding trades but no bars still returns rows, so this is the
    shape most likely to be mistaken for a healthy day.
    """
    day = date(2023, 5, 10)
    _land(tmp_path, day, datasets=("option_trades",))
    s3 = _S3(sessions=set())
    statuses, _ = ha.audit_range(
        _settings(tmp_path), day, day, _logger(), s3_factory=lambda: s3,
    )
    assert [s.verdict for s in statuses] == [ha.PARTIAL]
    assert statuses[0].missing == ["option_day_bars", "option_minute_bars"]
    assert s3.calls == [], "a partially-landed day must not need the vendor"


def test_complete_day_never_touches_the_vendor(tmp_path: Path) -> None:
    day = date(2023, 5, 10)
    _land(tmp_path, day)
    s3 = _S3(sessions={day.isoformat()})
    statuses, _ = ha.audit_range(
        _settings(tmp_path), day, day, _logger(), s3_factory=lambda: s3,
    )
    assert [s.verdict for s in statuses] == [ha.OK]
    assert s3.calls == []


# ---------------------------------------------------------------------------
# Empty partitions and unreadable files are absence, not presence
# ---------------------------------------------------------------------------

def test_empty_partition_directory_is_not_presence(tmp_path: Path) -> None:
    """--replace moves the old file aside first, so a dir can exist and be empty."""
    day = date(2023, 5, 10)
    for ds in ha.CLEAN_DATASETS:
        (tmp_path / "clean" / ds / f"dt={day.isoformat()}").mkdir(parents=True)
    s3 = _S3(sessions={day.isoformat()})
    statuses, _ = ha.audit_range(
        _settings(tmp_path), day, day, _logger(), s3_factory=lambda: s3,
    )
    assert [s.verdict for s in statuses] == [ha.GAP]


def test_zero_row_parquet_is_not_presence(tmp_path: Path) -> None:
    day = date(2023, 5, 10)
    for ds in ha.CLEAN_DATASETS:
        landing.write_clean(ds, day, [], job="flatfile_pull", data_root=tmp_path)
    s3 = _S3(sessions={day.isoformat()})
    statuses, _ = ha.audit_range(
        _settings(tmp_path), day, day, _logger(), s3_factory=lambda: s3,
    )
    assert [s.verdict for s in statuses] == [ha.GAP]


# ---------------------------------------------------------------------------
# The oracle must not guess
# ---------------------------------------------------------------------------

def test_non_404_error_is_unknown_not_holiday(tmp_path: Path) -> None:
    """A 403 or a network failure must never be recorded as a closed market."""
    from botocore.exceptions import ClientError

    class _Broken:
        def head_object(self, Bucket, Key):  # noqa: N803
            raise ClientError(
                {"Error": {"Code": "403"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
                "HeadObject",
            )

    statuses, calendar = ha.audit_range(
        _settings(tmp_path), date(2023, 2, 15), date(2023, 2, 15),
        _logger(), s3_factory=_Broken,
    )
    assert [s.verdict for s in statuses] == [ha.UNKNOWN]
    assert "2023-02-15" not in calendar, "an ambiguous answer must not be cached"


def test_offline_reports_unknown_rather_than_reaching_out(tmp_path: Path) -> None:
    statuses, _ = ha.audit_range(
        _settings(tmp_path), date(2023, 2, 15), date(2023, 2, 15),
        _logger(), offline=True,
    )
    assert [s.verdict for s in statuses] == [ha.UNKNOWN]


# ---------------------------------------------------------------------------
# Calendar cache
# ---------------------------------------------------------------------------

def test_cached_calendar_answers_without_a_second_probe(tmp_path: Path) -> None:
    ha.save_calendar({"2023-01-16": False}, tmp_path)
    s3 = _S3(sessions=set())
    statuses, _ = ha.audit_range(
        _settings(tmp_path), date(2023, 1, 16), date(2023, 1, 16),
        _logger(), s3_factory=lambda: s3,
    )
    assert [s.verdict for s in statuses] == [ha.HOLIDAY]
    assert s3.calls == [], "the cached answer should stand in for the probe"


def test_calendar_round_trips(tmp_path: Path) -> None:
    ha.save_calendar({"2023-01-16": False, "2023-01-17": True}, tmp_path)
    assert ha.load_calendar(tmp_path) == {"2023-01-16": False, "2023-01-17": True}


def test_corrupt_calendar_is_ignored_not_fatal(tmp_path: Path) -> None:
    landing.meta_path(ha.CALENDAR_NAME, data_root=tmp_path).write_text("{oops")
    assert ha.load_calendar(tmp_path) == {}


# ---------------------------------------------------------------------------
# Exit status
# ---------------------------------------------------------------------------

def _args(**kw):
    """Namespace shaped like the one run_job hands to _main_fn."""
    from types import SimpleNamespace

    base = {"date": "2023-02-16", "start": "2023-02-15", "end": "2023-02-15",
            "offline": False, "dry_run": True}
    base.update(kw)
    return SimpleNamespace(**base)


def test_main_fn_raises_on_a_gap_so_the_job_exits_nonzero(tmp_path, monkeypatch) -> None:
    """Healthchecks and CI only ever notice a non-zero exit.

    Driving _main_fn rather than raising the error inline is the whole point:
    if the job stopped raising, an inline test would still pass while the
    monitored run went green straight through a hole in the archive.
    """
    gap = ha.DayStatus(date(2023, 2, 15), ha.GAP,
                       dict.fromkeys(ha.CLEAN_DATASETS, False))
    monkeypatch.setattr(ha, "audit_range", lambda *a, **k: ([gap], {}))
    with pytest.raises(ha.HistoryGapError) as exc:
        ha._main_fn(_args(), _settings(tmp_path), _logger())
    assert "2023-02-15" in str(exc.value)


def test_main_fn_raises_on_a_partial_day_too(tmp_path, monkeypatch) -> None:
    partial = ha.DayStatus(date(2023, 2, 15), ha.PARTIAL,
                           {"option_trades": True, "option_minute_bars": False,
                            "option_day_bars": False})
    monkeypatch.setattr(ha, "audit_range", lambda *a, **k: ([partial], {}))
    with pytest.raises(ha.HistoryGapError):
        ha._main_fn(_args(), _settings(tmp_path), _logger())


def test_main_fn_is_quiet_when_the_archive_is_whole(tmp_path, monkeypatch) -> None:
    """A holiday is not a gap and must not fail the run."""
    days = [
        ha.DayStatus(date(2023, 2, 15), ha.OK, dict.fromkeys(ha.CLEAN_DATASETS, True)),
        ha.DayStatus(date(2023, 2, 20), ha.HOLIDAY,
                     dict.fromkeys(ha.CLEAN_DATASETS, False)),
    ]
    monkeypatch.setattr(ha, "audit_range", lambda *a, **k: (days, {}))
    summary = ha._main_fn(_args(), _settings(tmp_path), _logger())
    assert summary["gaps"] == 0
    assert summary["sessions"] == 1, "a holiday is not a session"


def test_unknown_does_not_fail_the_run(tmp_path, monkeypatch) -> None:
    """An unreachable vendor is reported, not treated as a missing day."""
    unknown = ha.DayStatus(date(2023, 2, 15), ha.UNKNOWN,
                           dict.fromkeys(ha.CLEAN_DATASETS, False))
    monkeypatch.setattr(ha, "audit_range", lambda *a, **k: ([unknown], {}))
    summary = ha._main_fn(_args(), _settings(tmp_path), _logger())
    assert summary["unknown"] == 1 and summary["gaps"] == 0


def test_render_lists_every_problem_day(tmp_path: Path) -> None:
    statuses = [
        ha.DayStatus(date(2023, 2, 15), ha.GAP, dict.fromkeys(ha.CLEAN_DATASETS, False)),
        ha.DayStatus(date(2023, 2, 16), ha.OK, dict.fromkeys(ha.CLEAN_DATASETS, True)),
    ]
    out = ha._render(date(2023, 2, 15), date(2023, 2, 16), statuses)
    assert "OK=1" in out and "GAP=1" in out
    # Only the per-day problem lines, not the header or the counts summary.
    listed = [ln for ln in out.splitlines()
              if re.match(r"^[A-Z]+\s+\d{4}-\d{2}-\d{2}", ln)]
    assert listed == ["GAP      2023-02-15  no data"], \
        "OK days should not clutter the report"


def test_json_payload_records_only_problem_days(tmp_path: Path) -> None:
    day = date(2023, 2, 15)
    s3 = _S3(sessions={day.isoformat()})
    statuses, calendar = ha.audit_range(
        _settings(tmp_path), day, day, _logger(), s3_factory=lambda: s3,
    )
    payload = {
        "days": [
            {"date": s.day.isoformat(), "verdict": s.verdict, "missing": s.missing}
            for s in statuses if s.verdict != ha.OK
        ]
    }
    assert payload["days"] == [{
        "date": "2023-02-15", "verdict": ha.GAP,
        "missing": sorted(ha.CLEAN_DATASETS),
    }]
    assert json.dumps(payload)  # must be serialisable for _meta/
