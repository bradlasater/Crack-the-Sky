"""Session calendar: is a date a trading session, and how many sessions apart.

Counting the sessions between today and an expiry 5-45 days out is a question
about *future* dates, and neither calendar file under ``_meta`` answers it
alone:

* ``trading_days.json`` (``ingest.jobs.history_audit``) is vendor-attested but
  purely historical. It accumulates by asking whether a flat file exists for a
  *past* date, so it holds nothing after the last audit -- 1048 weekdays,
  2022-08-31 through 2026-09-04, and not one day beyond.
* ``holidays.json`` (``ingest.jobs.holidays_sync``) looks forward but is
  partial: it is the ``/v1/marketstatus/upcoming`` window, naming closures
  ahead of its fetch and nothing behind it.

This module unions the two -- attested history where it exists,
weekday-minus-holidays across the forward window -- and raises outside both.

The raise is the point. A wrong session count does not announce itself: it
shifts T by one session, which moves every IV and every theta in the same
direction at once, and every number stays plausible. Answering "probably a
session" for a date nothing attests to is the failure worth being loud about,
so :class:`CalendarRangeError` is raised where a default would be guessed.

Weekends are settled before either source is consulted, because Saturday and
Sunday are never sessions and consulting a source about them would invent a
coverage gap where none exists. ``history_audit`` runs Saturday 13:00 ET,
verifying through Friday; ``holidays_sync`` runs Sunday 07:00 ET, so its
window opens that Sunday. In the healthy steady state the only date falling
between attested history and the forward window is the Saturday in between --
which needs no coverage. A *weekday* in that gap means one of the two jobs is
behind, and that is exactly when this module should refuse to answer.

The forward window opens on the day ``holidays_sync`` last ran, not on its
first record. A record list whose earliest entry is in November proves there
is no closure before November only if the list was fetched before November;
fetched after, a passed holiday has simply dropped off the upcoming window and
weekday-minus-holidays would call it a session. The fetch date is therefore
read from the newest ``raw/holidays/dt=`` partition -- the raw zone is never
rewritten, and ``scripts/prune_raw.sh`` keeps this dataset by name ("tiny
reference data").

Session *counts* are all this module produces. Turning a count into a year
fraction is the caller's business and is deliberately not decided here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ingest.common.market_gate import (
    is_weekday,
    load_calendar,
    load_early_closes,
    load_holidays,
)

RAW_HOLIDAYS_DATASET = "holidays"


class CalendarError(Exception):
    """Base for session-calendar failures."""


class CalendarRangeError(CalendarError):
    """A date no source attests to. Never answered with a guess."""


class CalendarSourceError(CalendarError):
    """The on-disk calendar sources are missing or unusable."""


@dataclass(frozen=True, slots=True)
class SessionCalendar:
    """Sessions from attested history plus a bounded forward window.

    ``sessions`` is the attested ``{date: was_a_session}`` map. ``holidays``
    and ``early_closes`` are the vendor's upcoming closures, which apply only
    within ``[forward_from, forward_through]`` -- respectively the day the
    holiday list was fetched and its last record. Nothing outside attested
    history and that window is answerable.
    """

    sessions: Mapping[date, bool]
    holidays: frozenset[date]
    early_closes: frozenset[date]
    forward_from: date
    forward_through: date

    def is_session(self, d: date) -> bool:
        """True when ``d`` is a trading session. Raises if nothing attests to it."""
        if not is_weekday(d):
            return False
        attested = self.sessions.get(d)
        if attested is not None:
            return attested
        if self.forward_from <= d <= self.forward_through:
            return d not in self.holidays
        raise CalendarRangeError(
            f"{d} is not covered: attested history holds "
            f"{self._attested_span()} and the holiday window runs "
            f"{self.forward_from}..{self.forward_through}"
        )

    def is_early_close(self, d: date) -> bool:
        """True when ``d`` is a session that closes early (13:00 ET).

        Only answerable inside the holiday window. ``trading_days.json``
        records whether a past date was a session, not how long it ran, so
        early closes before the window are recorded nowhere in the archive
        and this raises rather than reporting a half day as a full one.
        """
        if not self.forward_from <= d <= self.forward_through:
            raise CalendarRangeError(
                f"{d} is outside the holiday window "
                f"{self.forward_from}..{self.forward_through}; early closes "
                "before it are not recorded anywhere in the archive"
            )
        return self.is_session(d) and d in self.early_closes

    def sessions_between(self, start: date, end: date) -> int:
        """Sessions in the half-open interval ``(start, end]``.

        Half-open so that a same-day expiry counts zero sessions and each
        session is counted once when intervals are chained. Raises if any date
        in the interval is uncovered -- a partial count would be worse than no
        count, because it is indistinguishable from a short month.
        """
        if end < start:
            raise ValueError(f"end {end} precedes start {start}")
        count = 0
        d = start + timedelta(days=1)
        while d <= end:
            if self.is_session(d):
                count += 1
            d += timedelta(days=1)
        return count

    def _attested_span(self) -> str:
        if not self.sessions:
            return "nothing"
        return f"{min(self.sessions)}..{max(self.sessions)}"


def _default_data_root() -> Path:
    return Path(os.environ.get("DATA_ROOT", "/data/massive"))


def _newest_raw_partition(
    dataset: str, data_root: str | os.PathLike[str] | None = None
) -> date | None:
    """Latest ``dt=`` partition under ``raw/{dataset}``, or None if there is none."""
    root = (Path(data_root) if data_root is not None else _default_data_root())
    try:
        entries = list((root / "raw" / dataset).iterdir())
    except OSError:
        return None
    newest: date | None = None
    for path in entries:
        if not path.name.startswith("dt="):
            continue
        try:
            day = date.fromisoformat(path.name[3:])
        except ValueError:
            continue
        if newest is None or day > newest:
            newest = day
    return newest


def load_session_calendar(
    data_root: str | os.PathLike[str] | None = None,
) -> SessionCalendar:
    """Build the calendar from ``_meta`` and the raw holidays landing zone."""
    sessions: dict[date, bool] = {}
    for key, was_session in load_calendar(data_root).items():
        try:
            sessions[date.fromisoformat(key)] = was_session
        except ValueError:
            # An unparseable key names no date at all, so it attests to nothing
            # and cannot be the sole record for a day: dropping it loses no
            # coverage, and the rest of the file still loads. A *missing*
            # attested date is the different case, and is left uncovered so
            # that asking about it raises rather than guessing.
            continue

    holidays = load_holidays(data_root)
    early_closes = load_early_closes(data_root)
    forward_through = max(holidays | early_closes, default=None)
    if forward_through is None:
        raise CalendarSourceError(
            "no holiday records under _meta/holidays.json; the forward window "
            "cannot be bounded (is holidays_sync running?)"
        )

    forward_from = _newest_raw_partition(RAW_HOLIDAYS_DATASET, data_root)
    if forward_from is None:
        raise CalendarSourceError(
            f"no raw/{RAW_HOLIDAYS_DATASET}/dt= partition, so the date the "
            "holiday list was fetched is unknown; without it a closure that "
            "has dropped off the upcoming window would read as a session"
        )

    return SessionCalendar(
        sessions=sessions,
        holidays=frozenset(holidays),
        early_closes=frozenset(early_closes),
        forward_from=forward_from,
        forward_through=forward_through,
    )
