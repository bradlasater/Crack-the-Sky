"""Tests for reconcile: the flat file versus what the websocket actually saw.

There were none, which is why the websocket half of this job could compare the
authoritative record against zero every day and still report success.
"""

from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path

import pytest

from ingest.common import landing
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import read_partition, reconcile
from ingest.jobs.ws_minute_bars import DATASET as WS_RAW_DATASET

RUN_DATE = date(2026, 9, 1)


def _raw_dir(root: Path) -> Path:
    part = root / "raw" / WS_RAW_DATASET / f"dt={RUN_DATE.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    return part


def _record(sym: str, volume: float) -> dict:
    return {
        "sym": sym, "v": volume, "av": 100, "op": 1.0, "vw": 1.0, "o": 1.0,
        "c": 1.0, "h": 1.0, "l": 1.0, "a": 1.0, "z": 1,
        "s": 1788277440000, "e": 1788277500000, "recv_ms": 1788278402085,
    }


def _write_plain(root: Path, name: str, records: list[dict]) -> None:
    path = _raw_dir(root) / name
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


def _write_gz(root: Path, name: str, records: list[dict]) -> None:
    path = _raw_dir(root) / name
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def test_no_capture_directory_is_not_an_error(tmp_path: Path) -> None:
    assert reconcile._ws_raw_stats(tmp_path, RUN_DATE) is None


def test_empty_capture_directory_is_not_an_error(tmp_path: Path) -> None:
    _raw_dir(tmp_path)
    assert reconcile._ws_raw_stats(tmp_path, RUN_DATE) is None


def test_counts_rows_tickers_and_volume(tmp_path: Path) -> None:
    _write_plain(tmp_path, "ws_minute_bars-1.jsonl", [
        _record("O:SPY1", 3), _record("O:SPY2", 7),
    ])
    stats = reconcile._ws_raw_stats(tmp_path, RUN_DATE)
    assert stats == {"rows": 2, "tickers": 2, "volume": 10}


def test_reads_the_rotated_gzip_files_too(tmp_path: Path) -> None:
    """Files are gzipped on rotation, so by T+1 they all look like this."""
    _write_gz(tmp_path, "ws_minute_bars-1.jsonl.gz", [_record("O:SPY1", 5)])
    _write_plain(tmp_path, "ws_minute_bars-2.jsonl", [_record("O:SPY2", 4)])
    stats = reconcile._ws_raw_stats(tmp_path, RUN_DATE)
    assert stats["rows"] == 2
    assert stats["volume"] == 9


def test_repeated_tickers_count_once(tmp_path: Path) -> None:
    _write_plain(tmp_path, "ws_minute_bars-1.jsonl", [
        _record("O:SPY1", 1), _record("O:SPY1", 2), _record("O:SPY2", 3),
    ])
    stats = reconcile._ws_raw_stats(tmp_path, RUN_DATE)
    assert stats["rows"] == 3
    assert stats["tickers"] == 2


def test_files_that_parse_to_nothing_raise_rather_than_report_zero(tmp_path: Path) -> None:
    """The whole bug in one assertion.

    Reporting 0 rows for a day whose capture wrote 290,189 events made the
    comparison vacuous: flat-file-versus-zero always "reconciles", and the
    delta looks like a complete win. A reader that no longer understands the
    format on disk must say so.
    """
    path = _raw_dir(tmp_path) / "ws_minute_bars-1.jsonl"
    path.write_text("not json\n{\"nope\": 1}\n", encoding="utf-8")
    with pytest.raises(reconcile.UnreadableWsCapture, match="parsed to 0 records"):
        reconcile._ws_raw_stats(tmp_path, RUN_DATE)


def test_a_frame_shaped_archive_still_reads(tmp_path: Path) -> None:
    """Older captures held raw frames; they must not trip the zero-row guard."""
    path = _raw_dir(tmp_path) / "ws_minute_bars-1.jsonl"
    path.write_text(json.dumps([
        {"ev": "AM", "sym": "O:SPY1", "v": 6, "s": 1, "e": 2},
    ]) + "\n", encoding="utf-8")
    stats = reconcile._ws_raw_stats(tmp_path, RUN_DATE)
    assert stats["rows"] == 1
    assert stats["volume"] == 6


def test_null_volume_does_not_break_the_sum(tmp_path: Path) -> None:
    rec = _record("O:SPY1", 0)
    rec["v"] = None
    _write_plain(tmp_path, "ws_minute_bars-1.jsonl", [rec])
    assert reconcile._ws_raw_stats(tmp_path, RUN_DATE)["volume"] == 0


# ---------------------------------------------------------------------------
# The partition rewrite: flat file wins, but a failed write must not destroy
# ---------------------------------------------------------------------------

def _settings(data_root: Path) -> Settings:
    return Settings(
        massive_api_key="test-key",
        data_root=data_root,
        log_root=data_root / "logs",
    )


def _logger() -> JsonlLogger:
    return JsonlLogger(path=None, echo=False)


def _bar(ticker: str, src: str, volume: float = 1.0) -> dict:
    return {"ticker": ticker, "volume": volume, "src": src}


def _args(**kw):
    """Namespace shaped like the one run_job hands to _main."""
    from types import SimpleNamespace

    base = {"date": RUN_DATE.isoformat(), "dry_run": False}
    base.update(kw)
    return SimpleNamespace(**base)


def _seed_partition(root: Path, records: list[dict]) -> None:
    landing.write_clean(
        reconcile.CLEAN_DATASET, RUN_DATE, records,
        job="flatfile_pull", data_root=root,
    )


def test_rewrite_quarantines_prior_files_instead_of_deleting(tmp_path: Path) -> None:
    """The superseded files are moved aside, not deleted -- a delete cannot be
    undone if the new file turns out to be bad."""
    settings = _settings(tmp_path)
    _seed_partition(tmp_path, [_bar("O:SPY1", "flatfile"), _bar("O:SPY2", "ws")])

    out = reconcile._main(_args(), settings, _logger())

    assert out["reconciled"] is True
    rows = read_partition(settings, reconcile.CLEAN_DATASET, RUN_DATE)
    assert [r["src"] for r in rows] == ["flatfile"], \
        "the partition must hold exactly the flat-file rows afterwards"
    quarantined = list(
        (tmp_path / "_quarantine" / "refilter" / reconcile.CLEAN_DATASET
         / f"dt={RUN_DATE.isoformat()}").glob("*.parquet")
    )
    assert len(quarantined) == 1, "the prior file should be recoverable"


def test_failed_rewrite_preserves_the_existing_partition(tmp_path: Path, monkeypatch) -> None:
    """The regression: delete-then-write lost the whole partition when the
    replacement write failed (e.g. disk exhaustion mid-rewrite)."""
    settings = _settings(tmp_path)
    _seed_partition(tmp_path, [_bar("O:SPY1", "flatfile")])

    def _boom(*a, **k):
        raise OSError("No space left on device")

    monkeypatch.setattr(reconcile.landing, "write_clean", _boom)
    with pytest.raises(OSError):
        reconcile._main(_args(), settings, _logger())
    rows = read_partition(settings, reconcile.CLEAN_DATASET, RUN_DATE)
    assert len(rows) == 1, "the prior rows must survive a failed rewrite"


# ---------------------------------------------------------------------------
# main() argv handling
# ---------------------------------------------------------------------------

def test_main_keeps_an_equals_style_date(monkeypatch) -> None:
    """argparse accepts ``--date=X``; a bare "--date" membership test misses
    that form and the appended default would silently reconcile the wrong day."""
    seen: dict = {}
    monkeypatch.setattr(reconcile, "run_job",
                        lambda job, fn, argv: seen.setdefault("argv", argv))
    reconcile.main(["--date=2026-08-28", "--dry-run"])
    assert seen["argv"] == ["--date=2026-08-28", "--dry-run"]


def test_main_defaults_date_to_the_previous_trading_day(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(reconcile, "run_job",
                        lambda job, fn, argv: seen.setdefault("argv", argv))
    reconcile.main([])
    assert seen["argv"][0] == "--date"
