"""grouped_daily: market rows without a single wanted ticker is a failed run.

A genuinely empty response (holiday forced through the gate, a wrong date)
stays green: it lands ``grouped_empty`` and ``rows: 0`` as before. But a
response full of market rows in which SPY/VIXY/UVXY/VXX never appear means
the request or the feed is broken -- that used to exit 0 behind the same
``grouped_empty`` log, and now raises so ``run_job`` pings /fail and exits 1.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import grouped_daily as gd


def _settings(data_root: Path) -> Settings:
    return Settings(
        massive_api_key="test-key",
        data_root=data_root,
        log_root=data_root / "logs",
    )


def _args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "date": "2026-09-02", "force": True, "limit": None,
        "dry_run": True, "underlying": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


class _Client:
    """Stands in for MassiveClient: one scripted grouped-daily body."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        return self.body


def _bar(ticker: str) -> dict[str, Any]:
    return {"T": ticker, "t": 1788321600000, "o": 1.0, "h": 2.0, "l": 0.5,
            "c": 1.5, "v": 100, "vw": 1.25, "n": 10}


def _run(tmp_path, monkeypatch, body, keep_all: bool = False, **kw: Any):
    monkeypatch.setattr(gd, "MassiveClient", lambda *a, **k: _Client(body))
    logger = JsonlLogger(echo=False)
    try:
        return gd._main_fn(_args(**kw), _settings(tmp_path), logger, keep_all)
    finally:
        logger.close()


def test_results_without_any_wanted_ticker_fail(tmp_path, monkeypatch):
    body = {"results": [_bar("AAPL"), _bar("MSFT"), _bar("NVDA")]}
    with pytest.raises(RuntimeError, match="none of the wanted tickers"):
        _run(tmp_path, monkeypatch, body)


def test_truly_empty_response_stays_green(tmp_path, monkeypatch):
    """Holidays never reach main_fn (the market gate exits 0 first), but a
    forced run on a closed day -- or a wrong date -- gets an empty response,
    and that is not a failure."""
    summary = _run(tmp_path, monkeypatch, {"results": []})
    assert summary == {"rows": 0}


def test_wanted_tickers_are_kept(tmp_path, monkeypatch):
    body = {"results": [_bar("AAPL"), _bar("SPY"), _bar("VIXY")]}
    summary = _run(tmp_path, monkeypatch, body)
    assert summary == {"rows": 2}


def test_wanted_tickers_land_on_disk(tmp_path, monkeypatch):
    body = {"results": [_bar("AAPL"), _bar("SPY")]}
    summary = _run(tmp_path, monkeypatch, body, dry_run=False)
    assert summary == {"rows": 1}
    part = tmp_path / "clean" / gd.DATASET / "dt=2026-09-02"
    assert list(part.glob("*.parquet"))


def test_keep_all_matches_everything(tmp_path, monkeypatch):
    """--all keeps the whole market, so a non-empty response can never
    trigger the no-match failure."""
    body = {"results": [_bar("AAPL"), _bar("MSFT")]}
    summary = _run(tmp_path, monkeypatch, body, keep_all=True)
    assert summary == {"rows": 2}
