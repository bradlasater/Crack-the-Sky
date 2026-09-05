"""Market-gate tests: weekends, frozen holidays, early closes, gating exits.

The frozen holiday calendar (tests/fixtures/holidays.json, derived from a real
/v1/marketstatus/upcoming capture) contains:
  * 2026-09-07 (Mon) Labor Day     - closed
  * 2026-11-26 (Thu) Thanksgiving  - closed
  * 2026-11-27 (Fri)               - early-close (13:00 ET)
"""

from __future__ import annotations

import importlib
import re
import shutil
from datetime import date
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


def test_option_capture_end_is_close_plus_the_derived_buffer(data_root: Path) -> None:
    day = date(2026, 9, 8)
    assert market_gate.option_capture_end_et(day, data_root) == (
        market_gate.market_close_et(day, data_root) + market_gate.OPTION_CAPTURE_BUFFER
    )
    # Early closes move the deadline with the close, not to a fixed clock time.
    early = market_gate.option_capture_end_et(date(2026, 11, 27), data_root)
    assert (early.hour, early.minute) == (13, 35)


def test_capture_deadline_outlives_the_last_option_bar_delivery(data_root: Path) -> None:
    """The regression that made this a derived constant.

    A "generous-looking" close+20 stopped capture at 16:20 while the delayed
    feed was still delivering 16:04-16:15, so the closing minutes were dropped
    every session. Assert the property that was actually wanted: the deadline
    must survive until the *last* option bar has been delivered.
    """
    day = date(2026, 9, 8)
    close = market_gate.market_close_et(day, data_root)
    # Window start of the final option bar, delivered a feed-delay plus its
    # own one-minute window later.
    last_bar_start = close + market_gate.OPTION_CLOSE_LAG - market_gate.WS_BAR_WINDOW
    delivered = last_bar_start + market_gate.WS_FEED_DELAY + market_gate.WS_BAR_WINDOW
    assert market_gate.option_capture_end_et(day, data_root) >= delivered


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


# ---------------------------------------------------------------------------
# Weekend-only cron lines vs. the gate
# ---------------------------------------------------------------------------
#
# The gate exits 0 *quietly* so market holidays do not page, and run_job
# answers that exit with a Healthchecks success ping for the same reason. Put
# those two together on a job scheduled only on a Saturday or a Sunday and you
# get a job that can never run and monitors as healthy forever -- which is
# precisely what `contracts_sync --expired` did until 2026-09-05, having never
# once written a clean/contracts_expired partition.
#
# So: every weekend-only line in deploy/crontab must reach its work, either by
# forcing past the gate (holidays_sync, contracts_sync --expired) or by
# resolving its run date to a trading day (history_audit).

REPO_ROOT = Path(__file__).resolve().parent.parent
WEEKEND_DOW = {0, 6, 7}  # cron accepts both 0 and 7 for Sunday
DOW_NAMES = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}


def _dow_days(spec: str) -> set[int]:
    """Day numbers a cron day-of-week field selects.

    Expanded rather than string-matched. Splitting on commas alone reads
    ``6-7`` as the single token "6-7", which is not in WEEKEND_DOW, so a
    Saturday-and-Sunday line would drop out of the guard below while still
    looking covered -- and ranges are this crontab's normal style (``1-5``,
    ``2-6``). Steps and the SUN..SAT names are handled for the same reason.
    """
    days: set[int] = set()
    for part in spec.upper().split(","):
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            step = int(raw_step)
        if part == "*":
            lo, hi = 0, 7
        elif "-" in part:
            raw_lo, _, raw_hi = part.partition("-")
            lo, hi = DOW_NAMES.get(raw_lo, raw_lo), DOW_NAMES.get(raw_hi, raw_hi)
            lo, hi = int(lo), int(hi)
        else:
            lo = hi = int(DOW_NAMES.get(part, part))
        days.update(range(lo, hi + 1, step))
    return days


def _weekend_only_cron_jobs() -> list[tuple[str, str, list[str]]]:
    """(line, module, args) for deploy/crontab lines that only ever fire on a weekend."""
    found = []
    for line in (REPO_ROOT / "deploy" / "crontab").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 6 or not fields[0][0].isdigit():
            continue  # env assignment, not a schedule
        days = _dow_days(fields[4])
        if not days or not days <= WEEKEND_DOW:
            continue
        m = re.search(r"-m (ingest\.jobs\.\w+|pricing\.\w+)((?: --[\w-]+)*)", line)
        if not m:
            continue  # a bash script, which does not use run_job
        found.append((line, m.group(1), m.group(2).split()))
    return found


def test_the_crontab_has_weekend_only_lines_to_check() -> None:
    """Guard the guard: a parser that silently matches nothing proves nothing."""
    assert len(_weekend_only_cron_jobs()) >= 2


@pytest.mark.parametrize(
    ("module_path", "args"),
    [(mod, args) for _, mod, args in _weekend_only_cron_jobs()],
    ids=[f"{mod.rsplit('.', 1)[-1]}{''.join(args)}" for _, mod, args in _weekend_only_cron_jobs()],
)
def test_a_weekend_only_job_survives_its_own_schedule(monkeypatch, module_path, args) -> None:
    """It must force past the gate, or gate on a date that is a trading day."""
    module = importlib.import_module(module_path)
    seen: list[str] = []
    monkeypatch.setattr(module, "run_job", lambda job, main_fn, argv: seen.extend(argv))

    module.main(list(args))

    gate_date = next(
        (a.split("=", 1)[1] for a in seen if a.startswith("--date=")),
        seen[seen.index("--date") + 1] if "--date" in seen else None,
    )
    assert "--force" in seen or (
        gate_date is not None and market_gate.is_trading_day(date.fromisoformat(gate_date))
    ), (
        f"{module_path} {' '.join(args)} is scheduled only on a weekend, but neither forces "
        f"past the trading-day gate nor resolves --date to a trading day, so every run will "
        f"exit 0 before doing any work -- and ping Healthchecks green while doing it."
    )


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("6", {6}),
        ("0", {0}),
        ("6,0", {0, 6}),
        ("6-7", {6, 7}),  # the form the comma-split guard used to miss
        ("SAT", {6}),
        ("SAT-SUN", set()),  # names do not wrap; cron reads this as empty
        ("1-5", {1, 2, 3, 4, 5}),
        ("2-6", {2, 3, 4, 5, 6}),
        ("*", {0, 1, 2, 3, 4, 5, 6, 7}),
        ("*/3", {0, 3, 6}),
    ],
)
def test_dow_expansion(spec, expected) -> None:
    """The guard is only as good as its day-of-week parser."""
    assert _dow_days(spec) == expected


def test_a_weekend_range_would_not_slip_past_the_guard() -> None:
    """`6-7` is weekend-only and must be seen as such, not skipped as unparsed."""
    assert _dow_days("6-7") <= WEEKEND_DOW
    assert not _dow_days("2-6") <= WEEKEND_DOW  # Tue-Sat is not weekend-only
