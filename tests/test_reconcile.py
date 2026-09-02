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

from ingest.jobs import reconcile
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
