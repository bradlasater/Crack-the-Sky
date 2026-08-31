"""Market-gate tests: weekends, frozen holidays, early closes, gating exits.

The frozen holiday calendar (tests/fixtures/holidays.json, derived from a real
/v1/marketstatus/upcoming capture) contains:
  * 2026-09-07 (Mon) Labor Day     - closed
  * 2026-11-26 (Thu) Thanksgiving  - closed
  * 2026-11-27 (Fri)               - early-close (13:00 ET)
"""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from ingest.common import market_gate
from tests.conftest import FIXTURES_DIR


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    """A DATA_ROOT containing the frozen _meta/holidays.json calendar."""
    meta = tmp_path / "_meta"
    meta.mkdir()
    shutil.copy(FIXTURES_DIR / "holidays.json", meta / "holidays.json")
    return tmp_path


def test_now_and_today_are_eastern() -> None:
    now = market_gate.now_et()
    assert now.tzinfo is not None
    assert "New_York" in str(now.tzinfo) or "York" in str(now.tzname())
    assert market_gate.today_et() == now.date()


def test_is_weekday() -> None:
    assert market_gate.is_weekday(date(2026, 9, 8))  # Tuesday
    assert not market_gate.is_weekday(date(2026, 9, 5))  # Saturday
    assert not market_gate.is_weekday(date(2026, 9, 6))  # Sunday


def test_weekend_is_not_trading_day(data_root: Path) -> None:
    assert not market_gate.is_trading_day(date(2026, 9, 5), data_root)


def test_holiday_is_not_trading_day(data_root: Path) -> None:
    assert market_gate.load_holidays(data_root) == {date(2026, 9, 7), date(2026, 11, 26)}
    assert not market_gate.is_trading_day(date(2026, 9, 7), data_root)
    assert not market_gate.is_trading_day(date(2026, 11, 26), data_root)


def test_plain_weekday_is_trading_day(data_root: Path) -> None:
    assert market_gate.is_trading_day(date(2026, 9, 8), data_root)


def test_early_close_is_trading_day_but_closes_1300(data_root: Path) -> None:
    early = date(2026, 11, 27)
    assert market_gate.is_trading_day(early, data_root)
    assert early in market_gate.load_early_closes(data_root)
    close = market_gate.market_close_et(early, data_root)
    assert (close.hour, close.minute) == (13, 0)


def test_regular_close_is_1600(data_root: Path) -> None:
    close = market_gate.market_close_et(date(2026, 9, 8), data_root)
    assert (close.hour, close.minute) == (16, 0)


def test_option_capture_end_is_close_plus_20min(data_root: Path) -> None:
    assert market_gate.option_capture_end_et(date(2026, 9, 8), data_root) == (
        market_gate.market_close_et(date(2026, 9, 8), data_root) + timedelta(minutes=20)
    )
    assert market_gate.option_capture_end_et(date(2026, 11, 27), data_root).hour == 13


def test_require_trading_day_exits_0_quietly_on_closed(data_root: Path, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        market_gate.require_trading_day(date(2026, 9, 7), data_root=data_root)
    assert excinfo.value.code == 0
    assert capsys.readouterr().out == ""  # quiet


def test_require_trading_day_passes_on_open_day(data_root: Path) -> None:
    market_gate.require_trading_day(date(2026, 9, 8), data_root=data_root)  # no exit


def test_require_trading_day_force_bypasses(data_root: Path) -> None:
    market_gate.require_trading_day(date(2026, 9, 7), force=True, data_root=data_root)


def test_missing_calendar_fails_open_on_weekday(tmp_path: Path) -> None:
    assert market_gate.is_trading_day(date(2026, 9, 8), tmp_path)  # no holidays.json
    assert not market_gate.is_trading_day(date(2026, 9, 5), tmp_path)  # still weekend
