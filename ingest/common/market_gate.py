"""US market calendar gate: ET clock, holiday cache, trading-day checks.

Holiday data lives at ``{DATA_ROOT}/_meta/holidays.json`` (maintained by the
``holidays_sync`` job from ``/v1/marketstatus/upcoming``). The file is a JSON
array of records like::

    {"date": "2026-11-26", "exchange": "NYSE", "name": "Thanksgiving",
     "status": "closed"}
    {"date": "2026-11-27", ..., "status": "early-close", "early_close": true,
     "close": "2026-11-27T18:00:00.000Z"}

Records carry ``status`` (``"closed"`` / ``"early-close"``) and optionally an
explicit ``early_close`` boolean; both spellings are honoured. When the file
is missing the gate fails *open* on weekdays (weekends are always closed).

All ``data_root`` parameters default to the ``DATA_ROOT`` environment
variable (``/data/massive`` if unset) so the SPEC signatures work bare.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo(os.environ.get("TZ_NAME", "America/New_York"))

REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
OPTION_CAPTURE_BUFFER = timedelta(minutes=20)

# Cache of parsed holidays.json keyed by resolved file path.
_holiday_cache: dict[Path, list[dict]] = {}


def _default_data_root() -> Path:
    return Path(os.environ.get("DATA_ROOT", "/data/massive"))


def now_et() -> datetime:
    """Current time in America/New_York (or ``TZ_NAME``)."""
    return datetime.now(ET)


def today_et() -> date:
    """Today's date on the ET clock."""
    return now_et().date()


def is_weekday(d: date) -> bool:
    """True when ``d`` is a Monday-Friday."""
    return d.weekday() < 5


def _holidays_file(data_root: str | os.PathLike[str] | None) -> Path:
    root = Path(data_root) if data_root is not None else _default_data_root()
    return root / "_meta" / "holidays.json"


def _load_records(data_root: str | os.PathLike[str] | None = None) -> list[dict]:
    """Read and cache the raw holiday records from _meta/holidays.json."""
    path = _holidays_file(data_root)
    if path not in _holiday_cache:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = []
        if isinstance(data, dict):  # tolerate {"holidays": [...]} wrappers
            data = data.get("holidays", [])
        _holiday_cache[path] = [r for r in data if isinstance(r, dict) and r.get("date")]
    return _holiday_cache[path]


def load_holidays(data_root: str | os.PathLike[str] | None = None) -> set[date]:
    """Dates on which the market is fully closed (from _meta/holidays.json)."""
    out: set[date] = set()
    for rec in _load_records(data_root):
        if str(rec.get("status", "")).lower() == "closed":
            out.add(date.fromisoformat(rec["date"]))
    return out


def load_early_closes(data_root: str | os.PathLike[str] | None = None) -> set[date]:
    """Dates with a 13:00 ET early close (status ``early-close`` or flag)."""
    out: set[date] = set()
    for rec in _load_records(data_root):
        status = str(rec.get("status", "")).lower().replace("_", "-")
        if status == "early-close" or rec.get("early_close"):
            out.add(date.fromisoformat(rec["date"]))
    return out


def is_trading_day(d: date, data_root: str | os.PathLike[str] | None = None) -> bool:
    """True when the market is open at all on ``d`` (weekday, not a holiday)."""
    return is_weekday(d) and d not in load_holidays(data_root)


def require_trading_day(
    d: date | None = None,
    force: bool = False,
    data_root: str | os.PathLike[str] | None = None,
) -> None:
    """Exit quietly (code 0) when ``d`` is not a trading day, unless forced.

    Cron schedules jobs on weekdays; holidays are skipped here without noise.
    """
    d = d or today_et()
    if force:
        return
    if not is_trading_day(d, data_root):
        sys.exit(0)


def previous_trading_day(
    ref: date, data_root: str | os.PathLike[str] | None = None
) -> date:
    """Most recent trading day strictly before ``ref`` (weekends/holidays skipped)."""
    d = ref - timedelta(days=1)
    while not is_trading_day(d, data_root):
        d -= timedelta(days=1)
    return d


def market_close_et(d: date, data_root: str | os.PathLike[str] | None = None) -> datetime:
    """Market close instant (ET) for ``d``: 13:00 on early closes, else 16:00."""
    close_t = EARLY_CLOSE if d in load_early_closes(data_root) else REGULAR_CLOSE
    return datetime.combine(d, close_t, tzinfo=ET)


def option_capture_end_et(d: date, data_root: str | os.PathLike[str] | None = None) -> datetime:
    """End of option-data capture for ``d``: market close + 20 min buffer.

    Covers the 15-minute delayed feed plus the 16:15 options close tail.
    """
    return market_close_et(d, data_root) + OPTION_CAPTURE_BUFFER
