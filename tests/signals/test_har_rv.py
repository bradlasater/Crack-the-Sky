"""HAR-RV: walk-forward fit, horizon grid, residual band, no lookahead."""

from __future__ import annotations

import importlib.util
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from ingest.common import landing
from ingest.common.config import Settings
from ingest.schemas import SCHEMAS
from signals.har_rv import (
    HORIZONS,
    MIN_TRAIN_ROWS,
    _fit,
    _target,
    build_for_date,
    forecast_rows,
    har_features,
    realized_variances,
    write_rows,
)

ROOT = Path(__file__).resolve().parents[2]


def _sessions(n: int, start: date = date(2026, 1, 5)) -> list[date]:
    """n consecutive weekdays (Mon-Fri), good enough without a holiday list."""
    days = []
    d = start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _spot_rows(sessions: list[date], spots: list[float]) -> list[dict]:
    return [
        {"date": d.isoformat(), "spot": s, "src": "bars"}
        for d, s in zip(sessions, spots, strict=True)
    ]


def _synthetic_spots(n: int, base_vol: float = 0.01) -> list[float]:
    """Deterministic spots with slowly varying daily vol, alternating signs."""
    spots = [760.0]
    for t in range(1, n):
        vol_t = base_vol * (1.0 + 0.6 * math.sin(t / 17.0))
        ret = vol_t if t % 2 else -vol_t
        spots.append(spots[-1] * math.exp(ret))
    return spots


def test_schema_has_the_documented_columns() -> None:
    names = [f.name for f in SCHEMAS["rv_forecast"]]
    assert names == [
        "date", "horizon", "log_rv_mean", "log_rv_sd", "rv_daily",
        "vol_ann", "vol_ann_p10", "vol_ann_p90", "n_train",
    ]


def test_realized_variances_are_squared_log_returns() -> None:
    sessions = _sessions(3)
    rows = _spot_rows(sessions, [100.0, 101.0, 100.0])
    series = realized_variances(rows)
    assert series[0]["rv"] is None
    assert series[1]["rv"] == pytest.approx(math.log(1.01) ** 2)
    assert series[2]["rv"] == pytest.approx(math.log(100.0 / 101.0) ** 2)


def test_realized_variances_sorts_and_skips_rows_without_spot() -> None:
    sessions = _sessions(3)
    rows = _spot_rows(sessions, [100.0, 101.0, 102.0])
    rows = [rows[2], rows[0], {"date": "2026-01-06", "spot": None}, rows[1]]
    series = realized_variances(rows)
    assert [r["date"] for r in series] == [d.isoformat() for d in sessions]
    assert series[2]["rv"] == pytest.approx(math.log(102.0 / 101.0) ** 2)


def test_gap_in_the_calendar_voids_the_surrounding_returns() -> None:
    """A missing session is a multi-day move, not a daily one."""
    sessions = _sessions(4)
    calendar = {d.isoformat(): True for d in sessions}
    missing = sessions.pop(2)
    calendar[missing.isoformat()] = True  # it was a session; the row is absent
    rows = _spot_rows(sessions, [100.0, 101.0, 103.0])
    series = realized_variances(rows, calendar)
    assert series[1]["rv"] == pytest.approx(math.log(1.01) ** 2)
    # 100 -> 101 is fine, but 101 -> 103 spans the missing session.
    assert series[2]["rv"] is None


def test_har_features_are_log_window_means() -> None:
    rv = np.array([0.0001 * (1.0 + i / 100.0) for i in range(30)])
    feats = har_features(rv, 29)
    assert feats is not None
    assert feats[0] == 1.0
    assert feats[1] == pytest.approx(math.log(rv[29]))
    assert feats[2] == pytest.approx(math.log(rv[25:30].mean()))
    assert feats[3] == pytest.approx(math.log(rv[8:30].mean()))
    assert har_features(rv, 20) is None  # monthly window needs 22


def test_har_features_refuse_gaps_and_zero_rv() -> None:
    rv = np.full(36, 0.0001)
    rv[10] = math.nan
    assert har_features(rv, 15) is None   # nan inside the monthly window
    assert har_features(rv, 31) is None   # monthly window [10..31] still holds it
    assert har_features(rv, 35) is not None  # nan aged out of every window
    rv = np.full(30, 0.0001)
    rv[29] = 0.0  # identical prints: a stale close, not zero vol
    assert har_features(rv, 29) is None


def test_target_is_log_mean_over_the_horizon() -> None:
    rv = np.full(10, 0.0004)
    assert _target(rv, 0, 5) == pytest.approx(math.log(0.0004))
    assert _target(rv, 5, 5) is None  # window would run past the series end
    rv[3] = math.nan
    assert _target(rv, 0, 5) is None


def test_fit_recovers_an_exact_linear_relation() -> None:
    rng = np.arange(50, dtype=float)
    X = np.stack([np.ones(50), np.sin(rng), np.cos(rng / 3), rng / 50]).T
    beta_true = np.array([0.5, -2.0, 1.0, 0.25])
    y = X @ beta_true
    beta, sigma = _fit(X, y)
    assert beta == pytest.approx(beta_true, abs=1e-10)
    assert sigma == pytest.approx(0.0, abs=1e-10)


def test_forecast_rows_shape_band_and_training_counts() -> None:
    sessions = _sessions(160)
    rows = _spot_rows(sessions, _synthetic_spots(160))
    out = forecast_rows(realized_variances(rows))
    assert out, "expected forecasts after the minimum training window"
    by_date: dict[str, list[dict]] = {}
    for r in out:
        by_date.setdefault(r["date"], []).append(r)
    first = min(by_date)
    last = max(by_date)
    # Features first complete at index 22 (rv[0] is null), and a training row
    # for horizon 3 needs its target to end by the origin -> first origin is
    # index 22 + 63 - 1 + 3 = 87.
    assert sessions.index(date.fromisoformat(first)) >= 21 + MIN_TRAIN_ROWS + 3
    assert {r["horizon"] for r in by_date[last]} == set(HORIZONS)
    for r in out:
        assert r["vol_ann_p10"] < r["vol_ann"] < r["vol_ann_p90"]
        assert r["rv_daily"] == pytest.approx(
            math.exp(r["log_rv_mean"] + 0.5 * r["log_rv_sd"] ** 2)
        )
        assert r["vol_ann"] == pytest.approx(math.sqrt(252 * r["rv_daily"]))
        assert 0.01 < r["vol_ann"] < 2.0
    # n_train grows by one per origin once every horizon's target fits.
    tails = [r for r in by_date[last] if r["horizon"] == HORIZONS[0]]
    prev = [r for r in by_date[sessions[-2].isoformat()] if r["horizon"] == HORIZONS[0]]
    assert tails[0]["n_train"] == prev[0]["n_train"] + 1


def test_forecast_rows_has_no_lookahead() -> None:
    """Truncating the series after an origin must not move that origin's row."""
    sessions = _sessions(200)
    rows = _spot_rows(sessions, _synthetic_spots(200))
    series = realized_variances(rows)
    full = forecast_rows(series)
    cut = 170
    trunc = forecast_rows(series[:cut])
    boundary = sessions[cut - 1].isoformat()
    full_rows = [r for r in full if r["date"] == boundary]
    trunc_rows = [r for r in trunc if r["date"] == boundary]
    assert full_rows and len(full_rows) == len(trunc_rows)
    for a, b in zip(full_rows, trunc_rows, strict=True):
        assert a == pytest.approx(b)


def test_min_train_gates_early_origins() -> None:
    sessions = _sessions(120)
    rows = _spot_rows(sessions, _synthetic_spots(120))
    out = forecast_rows(realized_variances(rows), min_train=200)
    assert out == []


def _seed_spy_spot(tmp_path: Path, sessions: list[date], spots: list[float]) -> Settings:
    settings = Settings(massive_api_key="k", data_root=tmp_path)
    for d, s in zip(sessions, spots, strict=True):
        landing.write_clean(
            "spy_spot",
            d,
            [{"date": d.isoformat(), "spot": s, "src": "bars"}],
            job="spy_spot",
            data_root=tmp_path,
        )
    return settings


def test_build_for_date_roundtrips_through_the_warehouse(tmp_path) -> None:
    sessions = _sessions(160)
    settings = _seed_spy_spot(tmp_path, sessions, _synthetic_spots(160))
    rows = build_for_date(settings, sessions[-1])
    assert {r["horizon"] for r in rows} == set(HORIZONS)
    write_rows(settings, sessions[-1], rows)
    from marketdata.catalog import read_partition

    got = read_partition("rv_forecast", sessions[-1], tmp_path).to_pylist()
    assert len(got) == len(HORIZONS)
    assert set(got[0]) == {f.name for f in SCHEMAS["rv_forecast"]}
    assert got[0]["date"] == sessions[-1].isoformat()


def test_build_for_date_ignores_partitions_landed_later(tmp_path) -> None:
    sessions = _sessions(170)
    settings = _seed_spy_spot(tmp_path, sessions[:160], _synthetic_spots(170)[:160])
    before = build_for_date(settings, sessions[159])
    _seed_spy_spot(tmp_path, sessions[160:], _synthetic_spots(170)[160:])
    after = build_for_date(settings, sessions[159])
    assert before == pytest.approx(after)


def test_build_for_date_without_history_is_empty(tmp_path) -> None:
    settings = Settings(massive_api_key="k", data_root=tmp_path)
    sessions = _sessions(30)
    _seed_spy_spot(tmp_path, sessions, _synthetic_spots(30))
    assert build_for_date(settings, sessions[-1]) == []


def test_archive_script_builds_and_skips(tmp_path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location(
        "build_rv_forecast", ROOT / "scripts" / "build_rv_forecast.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sessions = _sessions(160)
    settings = _seed_spy_spot(tmp_path, sessions, _synthetic_spots(160))
    monkeypatch.setattr(mod.Settings, "load", lambda: settings)

    assert mod.main([]) == 0
    origin_with_rows = [
        r["date"]
        for r in forecast_rows(realized_variances(_spot_rows(sessions, _synthetic_spots(160))))
    ]
    assert origin_with_rows
    first_origin = date.fromisoformat(min(origin_with_rows))
    assert mod.already_built(settings, first_origin)

    # A second run rebuilds nothing.
    written: list[date] = []
    real_write = mod.write_rows

    def counting_write(s, d, rows):  # noqa: ANN001
        written.append(d)
        return real_write(s, d, rows)

    monkeypatch.setattr(mod, "write_rows", counting_write)
    assert mod.main([]) == 0
    assert written == []
    assert mod.main(["--force"]) == 0
    assert written
