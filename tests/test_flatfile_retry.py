"""Retry classification and the self-heal sweep in `flatfile_pull`.

The bug these pin: on 2026-09-15 the 11:05 ET run targeted 2026-09-14, the
vendor had not published `minute_aggs_v1` or `day_aggs_v1` by the 12:00 ET
cutoff, and the job exited 0 with `datasets_missing: 2`. Nothing alerted and
nothing ever looked at that date again, so the archive carried a PARTIAL day
until `history_audit` tripped over it four days later.

Two halves to the fix, and both are easy to get wrong:

* a miss must be classified, because `--dry-run` misses all three datasets by
  design and a blanket "missing => fail" would ping /fail on every dry run;
* the sweep must not raise, or one permanently unfillable day would fail this
  job forever and mask the next real T-1 failure.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from ingest.common import landing, market_gate
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import flatfile_pull as fp

TRADES_CSV = (
    "ticker,conditions,correction,exchange,price,sip_timestamp,size\n"
    "O:SPY260918C00770000,209,0,312,6.87,1787923996366000000,1\n"
    "O:SPXW260918P07600000,232,0,302,41.5,1787924018759000001,2\n"
)

TARGET = date(2026, 9, 15)  # the run's T-1 target
LATE_DAY = date(2026, 9, 14)  # the day the vendor published late


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeS3:
    """Serves only the (dataset, date) keys it was given; 404s everything else.

    `forbid` makes a key raise 403 instead, for the not-entitled path.
    """

    def __init__(self, present: set[tuple[str, str]] | None = None,
                 forbid: set[tuple[str, str]] | None = None) -> None:
        self.present = present or set()
        self.forbid = forbid or set()
        self.heads: list[tuple[str, str]] = []
        self.gets: list[tuple[str, str]] = []

    @staticmethod
    def _parse(key: str) -> tuple[str, str]:
        parts = key.split("/")
        return parts[1], parts[-1].removesuffix(".csv.gz")

    def head_object(self, Bucket: str, Key: str):  # noqa: N803 - boto3 kwarg
        item = self._parse(Key)
        self.heads.append(item)
        if item in self.forbid:
            raise ClientError(
                {"Error": {"Code": "AccessDenied"},
                 "ResponseMetadata": {"HTTPStatusCode": 403}}, "HeadObject")
        if item in self.present:
            return {"ContentLength": 1}
        raise ClientError(
            {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadObject")

    def get_object(self, Bucket: str, Key: str):  # noqa: N803 - boto3 kwarg
        self.gets.append(self._parse(Key))
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            gz.write(TRADES_CSV.encode())
        return {"Body": io.BytesIO(buf.getvalue())}

    def list_objects_v2(self, **kw: object):
        # Credentials are fine; a 403 above is about the object, not the keys.
        return {"Contents": []}


def _settings(tmp_path: Path) -> Settings:
    return Settings(massive_api_key="k", data_root=tmp_path,
                    log_root=tmp_path / "logs")


def _args(**over: object) -> Any:
    base: dict[str, Any] = {"dry_run": False, "limit": None, "replace": False,
                            "force_download": False, "backfill": False,
                            "date": TARGET.isoformat()}
    base.update(over)
    return SimpleNamespace(**base)


def _log() -> JsonlLogger:
    return JsonlLogger(path=None, echo=False)


def _write_manifest(tmp_path: Path, rows: list[tuple[str, str]]) -> None:
    """Land manifest entries as (dataset, iso-date) pairs."""
    landing.meta_path("flatfile_manifest.json", data_root=tmp_path).write_text(
        json.dumps([
            {"dataset": ds, "date": day, "md5": hashlib.md5(day.encode()).hexdigest(),
             "bytes": 1, "rows_in": 1, "rows_kept": 1}
            for ds, day in rows
        ]), encoding="utf-8")


@pytest.fixture
def clock(monkeypatch):
    """Freeze the ET clock past the publish cutoff, and pin T-1 to TARGET.

    Past RETRY_UNTIL_ET so the late path resolves without a 300s sleep.
    """
    monkeypatch.setattr(market_gate, "now_et",
                        lambda: datetime(2026, 9, 16, 13, 0))
    monkeypatch.setattr(fp, "previous_trading_day", lambda _today=None: TARGET)
    monkeypatch.setattr(market_gate, "today_et", lambda: date(2026, 9, 16))


# ---------------------------------------------------------------------------
# Miss classification
# ---------------------------------------------------------------------------

def test_a_late_dataset_fails_the_run(tmp_path: Path, clock, monkeypatch) -> None:
    """The 2026-09-14 case: vendor silent past the cutoff must exit non-zero.

    Exiting 0 here is what made the original hole invisible -- no /fail ping,
    and the unit's Restart=on-failure never got a turn.
    """
    s3 = FakeS3(present={("trades_v1", TARGET.isoformat())})
    monkeypatch.setattr(fp, "_s3_client", lambda _s: s3)
    with pytest.raises(fp.FlatfilePullError) as exc:
        fp._main(_args(), _settings(tmp_path), _log())
    assert "minute_aggs_v1" in str(exc.value)
    assert "day_aggs_v1" in str(exc.value)


def test_dry_run_misses_everything_and_still_exits_clean(tmp_path: Path, clock,
                                                        monkeypatch) -> None:
    """--dry-run misses all three by design; it must not ping /fail.

    This is the trap in a blanket `if datasets_missing: raise`.
    """
    s3 = FakeS3(present={(ds, TARGET.isoformat()) for ds in fp.DATASETS})
    monkeypatch.setattr(fp, "_s3_client", lambda _s: s3)
    out = fp._main(_args(dry_run=True), _settings(tmp_path), _log())
    assert out["datasets_missing"] == 3
    assert out["datasets_ok"] == 0


def test_dry_run_on_an_unpublished_date_still_exits_clean(tmp_path: Path, clock,
                                                          monkeypatch) -> None:
    """A dry run must never classify LATE, however silent the vendor is.

    The sibling test above only covers objects that *exist*, so the dry-run
    branch was reached before the miss ever mattered. With nothing published,
    `_head_with_retry` returned LATE and `_main` raised before the dry-run
    check -- exit 1, and a /fail ping against the production check. Observed
    for real on 2026-09-21 while probing the retry fix by hand.
    """
    s3 = FakeS3(present=set())
    monkeypatch.setattr(fp, "_s3_client", lambda _s: s3)
    out = fp._main(_args(dry_run=True), _settings(tmp_path), _log())
    assert out["datasets_missing"] == 3
    assert out["datasets_ok"] == 0


def test_dry_run_does_not_wait_for_publication(tmp_path: Path, monkeypatch) -> None:
    """An inspection must not sleep until 12:00 ET the way the cron run does.

    Deliberately *before* RETRY_UNTIL_ET, without the `clock` fixture's late
    time, so a regression here hangs the suite on a real sleep rather than
    passing quietly.
    """
    monkeypatch.setattr(market_gate, "now_et", lambda: datetime(2026, 9, 16, 9, 0))
    monkeypatch.setattr(fp, "previous_trading_day", lambda _today=None: TARGET)
    monkeypatch.setattr(market_gate, "today_et", lambda: date(2026, 9, 16))

    def _no_sleep(_s):
        raise AssertionError("a dry run waited for publication")

    monkeypatch.setattr(fp.time, "sleep", _no_sleep)
    s3 = FakeS3(present=set())
    monkeypatch.setattr(fp, "_s3_client", lambda _s: s3)
    out = fp._main(_args(dry_run=True), _settings(tmp_path), _log())
    assert out["datasets_missing"] == 3


def test_a_not_entitled_dataset_does_not_fail_the_run(tmp_path: Path, clock,
                                                      monkeypatch) -> None:
    """A 403 on an object outside the plan will never succeed; alerting is noise."""
    s3 = FakeS3(present={("trades_v1", TARGET.isoformat())},
                forbid={(ds, TARGET.isoformat())
                        for ds in ("minute_aggs_v1", "day_aggs_v1")})
    monkeypatch.setattr(fp, "_s3_client", lambda _s: s3)
    out = fp._main(_args(), _settings(tmp_path), _log())
    assert out["datasets_missing"] == 2
    assert out["datasets_ok"] == 1


def test_an_absent_historical_date_does_not_fail_the_run(tmp_path: Path,
                                                         monkeypatch) -> None:
    """A backfill of a date the vendor never published is a miss, not a failure."""
    old = date(2023, 4, 3)
    monkeypatch.setattr(fp, "previous_trading_day", lambda _today=None: TARGET)
    monkeypatch.setattr(market_gate, "today_et", lambda: date(2026, 9, 16))
    s3 = FakeS3()
    monkeypatch.setattr(fp, "_s3_client", lambda _s: s3)
    out = fp._main(_args(date=old.isoformat()), _settings(tmp_path), _log())
    assert out["datasets_missing"] == 3
    # wait_for_publish is off for an old date, so nothing slept on the cutoff.
    assert all(day == old.isoformat() for _ds, day in s3.heads)


# ---------------------------------------------------------------------------
# Which recent days the sweep considers incomplete
# ---------------------------------------------------------------------------

def test_a_partly_landed_day_is_incomplete(tmp_path: Path) -> None:
    """One of three landed proves it was a session and two are missing."""
    _write_manifest(tmp_path, [("trades_v1", LATE_DAY.isoformat())])
    assert fp.incomplete_recent_days(tmp_path, TARGET) == [
        (LATE_DAY, ["minute_aggs_v1", "day_aggs_v1"])
    ]


def test_a_complete_day_is_not_swept(tmp_path: Path) -> None:
    _write_manifest(tmp_path, [(ds, LATE_DAY.isoformat()) for ds in fp.DATASETS])
    assert fp.incomplete_recent_days(tmp_path, TARGET) == []


def test_a_day_with_nothing_landed_is_left_to_history_audit(tmp_path: Path) -> None:
    """Nothing landed is ambiguous -- holiday or total miss -- so do not guess.

    Deciding it needs the trades-object HEAD oracle that history_audit owns.
    """
    _write_manifest(tmp_path, [(ds, "2026-09-11") for ds in fp.DATASETS])
    assert fp.incomplete_recent_days(tmp_path, TARGET) == []


def test_the_target_itself_is_excluded(tmp_path: Path) -> None:
    """`before` is exclusive: the caller pulls the target right afterwards."""
    _write_manifest(tmp_path, [("trades_v1", TARGET.isoformat())])
    assert fp.incomplete_recent_days(tmp_path, TARGET) == []


def test_the_lookback_bounds_a_cold_archive(tmp_path: Path) -> None:
    """A long-dormant archive must not turn one run into a full re-pull."""
    days = [date(2026, 8, d).isoformat() for d in range(3, 29)]
    _write_manifest(tmp_path, [("trades_v1", day) for day in days])
    found = fp.incomplete_recent_days(tmp_path, TARGET)
    assert len(found) == fp.BACKFILL_LOOKBACK
    # The most recent ones, not the oldest.
    assert found[-1][0] == date(2026, 8, 28)


def test_a_malformed_manifest_row_does_not_break_the_sweep(tmp_path: Path) -> None:
    _write_manifest(tmp_path, [("trades_v1", "not-a-date"),
                               ("trades_v1", LATE_DAY.isoformat())])
    assert fp.incomplete_recent_days(tmp_path, TARGET) == [
        (LATE_DAY, ["minute_aggs_v1", "day_aggs_v1"])
    ]


# ---------------------------------------------------------------------------
# The sweep itself
# ---------------------------------------------------------------------------

def test_the_sweep_repulls_a_missing_dataset(tmp_path: Path, clock) -> None:
    """The whole point: the next morning's run closes yesterday's hole."""
    _write_manifest(tmp_path, [(ds, LATE_DAY.isoformat())
                               for ds in ("minute_aggs_v1", "day_aggs_v1")])
    s3 = FakeS3(present={("trades_v1", LATE_DAY.isoformat())})
    out = fp._backfill(s3, _settings(tmp_path), TARGET, _log(), _args())
    assert out == {"backfilled": 1, "backfill_failed": 0}
    assert ("trades_v1", LATE_DAY.isoformat()) in s3.gets
    part = (tmp_path / "clean" / fp.CLEAN_DATASET["trades_v1"]
            / f"dt={LATE_DAY.isoformat()}")
    assert len(list(part.glob("*.parquet"))) == 1


def test_the_sweep_reports_but_does_not_raise(tmp_path: Path, clock) -> None:
    """A day the vendor will never fill must not fail this job forever.

    Raising here would mask the next real T-1 failure behind a stale one and
    burn the restart budget on a hole that is not going to close.
    """
    _write_manifest(tmp_path, [(ds, LATE_DAY.isoformat())
                               for ds in ("minute_aggs_v1", "day_aggs_v1")])
    s3 = FakeS3()  # serves nothing
    out = fp._backfill(s3, _settings(tmp_path), TARGET, _log(), _args())
    assert out == {"backfilled": 0, "backfill_failed": 1}


def test_a_complete_archive_sweeps_nothing(tmp_path: Path, clock) -> None:
    """A healthy archive must cost no extra S3 calls at all."""
    _write_manifest(tmp_path, [(ds, LATE_DAY.isoformat()) for ds in fp.DATASETS])
    s3 = FakeS3()
    assert fp._backfill(s3, _settings(tmp_path), TARGET, _log(), _args()) == {}
    assert s3.heads == []


# ---------------------------------------------------------------------------
# When the sweep runs at all
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], True),                                  # the unattended T-1 run
        (["--date", "2026-09-14"], False),           # someone on one day
        (["--date=2026-09-14"], False),              # same, single token
        (["--no-backfill"], False),                  # explicit opt-out
    ],
)
def test_who_gets_the_sweep(monkeypatch, argv: list[str], expected: bool) -> None:
    """An explicit --date must not pull nine other days out from under you."""
    seen: dict[str, Any] = {}

    def _fake_run_job(job, main_fn, a):
        main_fn(SimpleNamespace(date=TARGET.isoformat()), None, None)
        return 0

    monkeypatch.setattr(fp, "previous_trading_day", lambda _today=None: TARGET)
    monkeypatch.setattr(market_gate, "today_et", lambda: date(2026, 9, 16))
    monkeypatch.setattr(fp, "run_job", _fake_run_job)
    monkeypatch.setattr(fp, "_main", lambda a, st, log: seen.update(backfill=a.backfill))

    fp.main(argv)
    assert seen["backfill"] is expected


# ---------------------------------------------------------------------------
# Not re-writing what is already landed
# ---------------------------------------------------------------------------

def test_an_already_landed_dataset_is_not_rewritten(tmp_path: Path, clock) -> None:
    """The duplicate-partition bug: a re-run must not add a second parquet.

    reuse_local skips only the download; it still re-filters and writes a new
    timestamped file, which a whole-partition read double-counts. A repair.sh
    run for 2026-09-14 did exactly that -- 2,861,001 trades counted twice --
    and with a late sibling now failing the run, every systemd retry would
    have added another copy.
    """
    _write_manifest(tmp_path, [("trades_v1", LATE_DAY.isoformat())])
    s3 = FakeS3(present={("trades_v1", LATE_DAY.isoformat())})
    result = fp._pull_dataset(s3, _settings(tmp_path), "trades_v1", LATE_DAY,
                              _log(), _args())
    assert result.entry is not None and result.miss is None
    assert s3.heads == [] and s3.gets == []
    part = (tmp_path / "clean" / fp.CLEAN_DATASET["trades_v1"]
            / f"dt={LATE_DAY.isoformat()}")
    assert not part.exists()


def test_a_retry_of_a_partly_landed_day_stays_at_one_copy(tmp_path: Path,
                                                          clock, monkeypatch) -> None:
    """Two runs of a day whose sibling is late must leave one parquet, not two."""
    s3 = FakeS3(present={("trades_v1", TARGET.isoformat())})
    monkeypatch.setattr(fp, "_s3_client", lambda _s: s3)
    for _ in range(2):
        with pytest.raises(fp.FlatfilePullError):
            fp._main(_args(), _settings(tmp_path), _log())
    part = (tmp_path / "clean" / fp.CLEAN_DATASET["trades_v1"]
            / f"dt={TARGET.isoformat()}")
    assert len(list(part.glob("*.parquet"))) == 1
    assert s3.gets.count(("trades_v1", TARGET.isoformat())) == 1


def test_replace_still_rewrites(tmp_path: Path, clock) -> None:
    """refilter.sh passes --replace; that path must keep working."""
    _write_manifest(tmp_path, [("trades_v1", LATE_DAY.isoformat())])
    s3 = FakeS3(present={("trades_v1", LATE_DAY.isoformat())})
    result = fp._pull_dataset(s3, _settings(tmp_path), "trades_v1", LATE_DAY,
                              _log(), _args(replace=True))
    assert result.entry is not None and result.entry["rows_kept"] == 2
    assert s3.gets == [("trades_v1", LATE_DAY.isoformat())]
