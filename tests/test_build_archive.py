"""Tests for the archive rebuild scripts.

``scripts/build_surface.py`` and ``scripts/build_term_structure.py`` are the
only way to walk history, and they currently had none. The behaviours that
must not regress: ``already_built`` is a nonempty parquet partition,
``--force`` rebuilds those dates, and one bad date must not end the run.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

from ingest.common.config import Settings
from pricing.surface import SurfaceArbitrageError

ROOT = Path(__file__).resolve().parents[1]
SESSIONS = ["2026-08-26", "2026-08-27", "2026-08-28"]
CALENDAR = {d: True for d in SESSIONS}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _settings(tmp_path: Path) -> Settings:
    return Settings(massive_api_key="k", data_root=tmp_path, log_root=tmp_path / "logs")


def _land(tmp_path: Path, dataset: str, day: str) -> None:
    part = tmp_path / "clean" / dataset / f"dt={day}"
    part.mkdir(parents=True, exist_ok=True)
    (part / "x-1.parquet").write_bytes(b"parq")


@pytest.fixture(params=["build_surface.py", "build_term_structure.py"])
def archive(request):
    return _load(request.param)


def _install(mod, monkeypatch, tmp_path, behaviour):
    """Stub calendar + fit + write so main() is a control-flow test."""
    monkeypatch.setattr(mod.Settings, "load", lambda: _settings(tmp_path))
    monkeypatch.setattr(mod, "load_calendar", lambda _root: dict(CALENDAR))
    calls: list[date] = []
    is_surface = hasattr(mod, "rows_from_surfaces")

    def fake_build(settings, d, roots):  # noqa: ANN001
        calls.append(d)
        what = behaviour(d)
        if isinstance(what, BaseException):
            raise what
        if what == "empty":
            return {} if is_surface else []
        return {"SPXW": object()} if is_surface else [{"underlying": "SPXW"}]

    monkeypatch.setattr(mod, "build_for_date", fake_build)
    if is_surface:
        monkeypatch.setattr(mod, "rows_from_surfaces", lambda _s: [{"underlying": "SPXW"}])

    written: list[date] = []

    def fake_write(settings, d, rows):  # noqa: ANN001
        written.append(d)
        _land(tmp_path, mod.DATASET, d.isoformat())

    monkeypatch.setattr(mod, "write_rows", fake_write)
    return calls, written


def test_already_built_requires_a_parquet(archive, tmp_path: Path) -> None:
    s = _settings(tmp_path)
    d = date(2026, 8, 28)
    assert archive.already_built(s, d) is False
    part = tmp_path / "clean" / archive.DATASET / f"dt={d.isoformat()}"
    part.mkdir(parents=True)
    assert archive.already_built(s, d) is False
    (part / "x.parquet").write_bytes(b"x")
    assert archive.already_built(s, d) is True


def test_skips_dates_already_built(archive, tmp_path: Path, monkeypatch) -> None:
    _land(tmp_path, archive.DATASET, "2026-08-26")
    calls, written = _install(archive, monkeypatch, tmp_path, lambda _d: "ok")
    assert archive.main([]) == 0
    assert [d.isoformat() for d in calls] == ["2026-08-27", "2026-08-28"]
    assert [d.isoformat() for d in written] == ["2026-08-27", "2026-08-28"]


def test_force_rebuilds_dates_already_built(archive, tmp_path: Path, monkeypatch) -> None:
    _land(tmp_path, archive.DATASET, "2026-08-26")
    calls, written = _install(archive, monkeypatch, tmp_path, lambda _d: "ok")
    assert archive.main(["--force"]) == 0
    assert [d.isoformat() for d in calls] == SESSIONS
    assert [d.isoformat() for d in written] == SESSIONS


def test_one_bad_date_does_not_end_the_run(
    archive, tmp_path: Path, monkeypatch, capsys
) -> None:
    def behaviour(d: date):
        if d.isoformat() == "2026-08-27":
            return SurfaceArbitrageError("calendar arbitrage: test")
        return "ok"

    calls, written = _install(archive, monkeypatch, tmp_path, behaviour)
    assert archive.main([]) == 1
    assert [d.isoformat() for d in calls] == SESSIONS
    assert [d.isoformat() for d in written] == ["2026-08-26", "2026-08-28"]
    err = capsys.readouterr().err
    assert "2026-08-27 FAILED" in err
    assert "SurfaceArbitrageError" in err


def test_empty_date_is_reported_and_the_run_continues(
    archive, tmp_path: Path, monkeypatch, capsys
) -> None:
    def behaviour(d: date):
        return "empty" if d.isoformat() == "2026-08-27" else "ok"

    calls, written = _install(archive, monkeypatch, tmp_path, behaviour)
    assert archive.main([]) == 1
    assert [d.isoformat() for d in calls] == SESSIONS
    assert [d.isoformat() for d in written] == ["2026-08-26", "2026-08-28"]
    assert "2026-08-27 EMPTY" in capsys.readouterr().err


def test_missing_calendar_fails(archive, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(archive.Settings, "load", lambda: _settings(tmp_path))
    monkeypatch.setattr(archive, "load_calendar", lambda _root: {})
    assert archive.main([]) == 1
