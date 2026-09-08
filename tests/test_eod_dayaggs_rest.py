"""eod_dayaggs_rest: a 404 means the contract did not trade, not a failed run.

MassiveHTTPError carries ``status_code`` but never sets ``response`` (the
client builds it without one so the apiKey cannot leak through a response
body), so a skip check written against ``exc.response.status_code`` never
matched -- the first unknown/delisted contract re-raised and killed the whole
EOD sweep partway through.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
import requests

from ingest.common.config import Settings
from ingest.common.http_client import MassiveHTTPError
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import eod_dayaggs_rest as eod
from ingest.jobs.eod_dayaggs_rest import _fetch_day_bar


class _Client:
    """Stands in for MassiveClient: scripted body or failure."""

    def __init__(self, body: dict | None = None, exc: Exception | None = None) -> None:
        self.body = body or {}
        self.exc = exc

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        if self.exc is not None:
            raise self.exc
        return self.body


def test_404_means_the_contract_did_not_trade() -> None:
    client = _Client(exc=MassiveHTTPError(404, "https://x?apiKey=SECRET"))
    assert _fetch_day_bar(client, "O:SPY260918C00765000", "2026-09-01") is None


def test_empty_results_are_an_empty_list_not_none() -> None:
    """200 with no bar is distinct from 404: both skipped, counted apart."""
    assert _fetch_day_bar(_Client(body={"results": []}), "T", "2026-09-01") == []


def test_other_http_errors_still_raise() -> None:
    client = _Client(exc=MassiveHTTPError(500, "https://x?apiKey=SECRET"))
    with pytest.raises(requests.HTTPError):
        _fetch_day_bar(client, "T", "2026-09-01")


def test_a_plain_http_error_carrying_a_response_still_matches() -> None:
    resp = requests.Response()
    resp.status_code = 404
    client = _Client(exc=requests.HTTPError("404", response=resp))
    assert _fetch_day_bar(client, "T", "2026-09-01") is None


# ---------------------------------------------------------------------------
# Checkpointing: an interrupted full-universe sweep resumes, not restarts
# ---------------------------------------------------------------------------

_TICKERS = [f"O:SPY260918C00{i}00000" for i in range(5)]


def _sweep_settings(data_root: Path) -> Settings:
    return Settings(
        massive_api_key="test-key",
        data_root=data_root,
        log_root=data_root / "logs",
    )


def _sweep_args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "date": "2026-09-02", "force": True, "limit": None,
        "dry_run": False, "underlying": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _patch_universe(monkeypatch) -> None:
    monkeypatch.setattr(eod, "PROGRESS_EVERY", 2)  # checkpoint every 2 tickers
    monkeypatch.setattr(
        eod, "latest_contracts",
        lambda settings, on_or_before: [{"ticker": t} for t in _TICKERS],
    )


class _ScriptedClient:
    """MassiveClient stand-in: a bar per ticker, raising once at ``fail_at``."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.calls: list[str] = []
        self.fail_at = fail_at

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        ticker = path.split("/")[4]  # /v2/aggs/ticker/{t}/range/...
        self.calls.append(ticker)
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise requests.ConnectionError("simulated mid-sweep crash")
        return {"results": [{"t": 1788321600000, "o": 1.0, "h": 2.0, "l": 0.5,
                             "c": 1.5, "v": 100, "vw": 1.25, "n": 10}]}


def _run_sweep(tmp_path, monkeypatch, client: _ScriptedClient, **kw: Any):
    monkeypatch.setattr(eod, "MassiveClient", lambda *a, **k: client)
    logger = JsonlLogger(echo=False)
    try:
        return eod._main_fn(_sweep_args(**kw), _sweep_settings(tmp_path), logger, False)
    finally:
        logger.close()


def test_interrupted_sweep_resumes_from_checkpoint(tmp_path, monkeypatch):
    _patch_universe(monkeypatch)

    with pytest.raises(requests.ConnectionError):
        _run_sweep(tmp_path, monkeypatch, _ScriptedClient(fail_at=3))

    # The crash landed after 2 tickers (the third call raised): the checkpoint
    # holds them and their bars, so the restart must not refetch them.
    checkpoint = json.loads(
        (tmp_path / "_meta" / eod.CHECKPOINT_NAME).read_text(encoding="utf-8"))
    assert checkpoint["run_date"] == "2026-09-02"
    assert checkpoint["done"] == _TICKERS[:2]
    partial = tmp_path / "_meta" / eod.PARTIAL_NAME
    assert len(partial.read_text(encoding="utf-8").splitlines()) == 2

    client = _ScriptedClient()
    summary = _run_sweep(tmp_path, monkeypatch, client)
    assert client.calls == _TICKERS[2:], "resumed run refetches only the tail"
    assert summary["rows"] == 5
    assert summary["resumed"] == 2

    # All five bars land once each, and the checkpoint is cleaned up.
    clean_part = tmp_path / "clean" / "option_day_bars" / "dt=2026-09-02"
    assert list(clean_part.glob("*.parquet"))
    assert not (tmp_path / "_meta" / eod.CHECKPOINT_NAME).exists()
    assert not partial.exists()


def test_checkpoint_for_another_date_is_ignored(tmp_path, monkeypatch):
    """A stale checkpoint must restart the sweep, never skip contracts."""
    _patch_universe(monkeypatch)
    meta = tmp_path / "_meta"
    meta.mkdir(parents=True)
    (meta / eod.CHECKPOINT_NAME).write_text(
        json.dumps({"run_date": "2026-09-01", "done": _TICKERS[:3]}),
        encoding="utf-8",
    )
    client = _ScriptedClient()
    summary = _run_sweep(tmp_path, monkeypatch, client)
    assert client.calls == _TICKERS
    assert summary["resumed"] == 0


def test_dry_run_never_writes_checkpoint_state(tmp_path, monkeypatch):
    _patch_universe(monkeypatch)
    summary = _run_sweep(tmp_path, monkeypatch, _ScriptedClient(), dry_run=True)
    assert summary["rows"] == 5
    assert not (tmp_path / "_meta").exists()
