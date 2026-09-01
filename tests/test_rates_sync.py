"""rates_sync: resumability, and telling a quota apart from a broken endpoint.

These paths decide whether a backfill silently skips data or misreports it, so
they are covered explicitly rather than inferred from a green run.
"""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from ingest.common import landing
from ingest.common.config import Settings
from ingest.common.http_client import MassiveHTTPError
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import rates_sync as job


def _settings(tmp_path: Path) -> Settings:
    return dataclasses.replace(
        Settings(massive_api_key="k"), data_root=tmp_path, log_root=tmp_path / "logs"
    )


def _args(**kw) -> argparse.Namespace:
    base = {"date": "2026-09-01", "limit": None, "dry_run": False,
            "force": True, "underlying": None}
    base.update(kw)
    return argparse.Namespace(**base)


def _row(d: str, one_month: float = 3.84) -> dict:
    return {"date": d, "yield_1_month": one_month, "yield_3_month": 3.90,
            "yield_10_year": 4.73}


class _Client:
    """Stands in for MassiveClient: scripted pages, optional failure."""

    def __init__(self, pages: list[list[dict]], fail: Exception | None = None) -> None:
        self.pages = pages
        self.fail = fail
        self.calls: list[dict] = []

    def get(self, path, params=None):
        self.calls.append({"path": path, **(params or {})})
        return {"results": self.pages[0] if self.pages else []}

    def paginate(self, path, params=None, limit=None):
        self.calls.append({"path": path, **(params or {})})
        for page in self.pages:
            yield from page
        if self.fail is not None:
            raise self.fail


def _install(monkeypatch, client: _Client) -> None:
    monkeypatch.setattr(job, "MassiveClient", lambda *a, **k: client)


# ---------------------------------------------------------------------------
# Cursor file robustness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content", ["[]", "null", '"str"', "123", "not json", ""])
def test_malformed_cursor_does_not_break_the_run(content: str, tmp_path: Path) -> None:
    """Valid JSON of the wrong type used to raise on .items().

    The cursor is read on every run, incremental included, so this would have
    broken the daily job as well as the backfill.
    """
    settings = _settings(tmp_path)
    landing.meta_path(job.CURSOR_NAME, data_root=tmp_path).write_text(content)
    assert job._load_cursor(settings) == {}


def test_cursor_roundtrip(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    job._save_cursor(settings, {"treasury_yields": "1996-09-04"})
    assert job._load_cursor(settings) == {"treasury_yields": "1996-09-04"}


# ---------------------------------------------------------------------------
# Resume parameters
# ---------------------------------------------------------------------------

def test_full_walk_without_a_cursor_starts_from_the_top(monkeypatch, tmp_path) -> None:
    client = _Client([[_row("2026-08-28"), _row("2026-08-27")]])
    _install(monkeypatch, client)
    job._main_fn(_args(), _settings(tmp_path), JsonlLogger(path=None, echo=False), True)
    assert all("date.lt" not in c for c in client.calls)


def test_full_walk_resumes_from_the_cursor(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    job._save_cursor(settings, {"treasury_yields": "1996-09-04",
                                "inflation": "1947-01-01"})
    client = _Client([[_row("1996-09-03")]])
    _install(monkeypatch, client)
    job._main_fn(_args(), settings, JsonlLogger(path=None, echo=False), True)
    lts = {c["path"]: c.get("date.lt") for c in client.calls}
    assert lts["/fed/v1/treasury-yields"] == "1996-09-04"


def test_incremental_run_ignores_the_cursor(monkeypatch, tmp_path) -> None:
    """A daily run must fetch the newest page, not resume the history walk."""
    settings = _settings(tmp_path)
    job._save_cursor(settings, {"treasury_yields": "1996-09-04"})
    client = _Client([[_row("2026-08-28")]])
    _install(monkeypatch, client)
    job._main_fn(_args(), settings, JsonlLogger(path=None, echo=False), False)
    assert all("date.lt" not in c for c in client.calls)


def test_cursor_only_moves_backwards(monkeypatch, tmp_path) -> None:
    """A newer page must not drag the resume point forward and skip history."""
    settings = _settings(tmp_path)
    job._save_cursor(settings, {"treasury_yields": "1970-01-01"})
    client = _Client([[_row("2026-08-28")]])
    _install(monkeypatch, client)
    job._main_fn(_args(), settings, JsonlLogger(path=None, echo=False), True)
    assert job._load_cursor(settings)["treasury_yields"] == "1970-01-01"


# ---------------------------------------------------------------------------
# Quota vs a broken endpoint
# ---------------------------------------------------------------------------

def test_exhausted_429_is_a_resumable_stop(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    client = _Client([[_row("2000-01-04"), _row("2000-01-03")]],
                     fail=MassiveHTTPError(429, "https://x?apiKey=SECRET"))
    _install(monkeypatch, client)
    out = job._main_fn(_args(), settings, JsonlLogger(path=None, echo=False), True)
    assert out["rows"] > 0
    assert "treasury_yields" in (out["partial"] or "")
    # Progress is banked so the next run continues.
    assert job._load_cursor(settings)["treasury_yields"] == "2000-01-03"


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_permanent_http_errors_are_not_disguised_as_rate_limiting(
    status: int, monkeypatch, tmp_path
) -> None:
    """A broken endpoint must not be banked as progress and exit 0."""
    client = _Client([[_row("2000-01-04")]],
                     fail=MassiveHTTPError(status, "https://x?apiKey=SECRET"))
    _install(monkeypatch, client)
    with pytest.raises(MassiveHTTPError):
        job._main_fn(_args(), _settings(tmp_path), JsonlLogger(path=None, echo=False), True)


def test_rate_limit_error_never_carries_the_key() -> None:
    assert "SECRET" not in str(MassiveHTTPError(429, "https://x?apiKey=SECRET"))


# ---------------------------------------------------------------------------
# Completion and empties
# ---------------------------------------------------------------------------

def test_completed_history_lands_nothing_and_succeeds(monkeypatch, tmp_path) -> None:
    """Once walked back to the start, --full must not page every night."""
    settings = _settings(tmp_path)
    job._save_cursor(settings, {"treasury_yields": "1962-01-02",
                                "inflation": "1947-01-01"})
    _install(monkeypatch, _Client([[]]))
    out = job._main_fn(_args(), settings, JsonlLogger(path=None, echo=False), True)
    assert out["rows"] == 0
    assert "treasury_yields" in (out["history_complete"] or "")


def test_nothing_landed_and_nothing_complete_is_a_failure(monkeypatch, tmp_path) -> None:
    _install(monkeypatch, _Client([[]]))
    with pytest.raises(RuntimeError, match="landed no rows"):
        job._main_fn(_args(), _settings(tmp_path), JsonlLogger(path=None, echo=False), False)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing_including_the_cursor(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    _install(monkeypatch, _Client([[_row("2026-08-28")]]))
    job._main_fn(_args(dry_run=True), settings, JsonlLogger(path=None, echo=False), True)
    assert not (tmp_path / "clean").exists()
    assert not landing.meta_path(job.CURSOR_NAME, data_root=tmp_path).is_file()


def test_percent_values_land_unchanged(monkeypatch, tmp_path) -> None:
    """The job stores what the vendor quotes; pricing converts, not ingest."""
    settings = _settings(tmp_path)
    _install(monkeypatch, _Client([[_row("2026-08-28", one_month=3.84)]]))
    job._main_fn(_args(), settings, JsonlLogger(path=None, echo=False), False)
    f = next((tmp_path / "clean" / "treasury_yields").rglob("*.parquet"))
    row = pq.read_table(f).to_pylist()[0]
    assert row["yield_1_month"] == 3.84
    assert row["yield_6_month"] is None      # unpopulated tenor stays null
