"""Append-only writer for ``decision_log``. Never quarantines.

The strategy engine does not exist yet; the backtester is the first intended
caller. ``landing.write_clean`` already stamps a new file per write — this
module is the fail-loud record builder plus the rule that a batch is one
session and one job, so a mixed write cannot land events in the wrong
partition.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from ingest.common import landing
from ingest.schemas import APPEND_ONLY_DATASETS, decision_record
from marketdata import catalog

DATASET = "decision_log"


def write_decisions(
    records: Iterable[dict[str, Any]],
    *,
    job: str,
    data_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Append one session's decision rows. Returns the new parquet path.

    Refuses an empty batch and a mix of ``session_date`` or ``job`` values.
    Does not call ``quarantine_prior`` — a second write is a second file.
    """
    if DATASET not in APPEND_ONLY_DATASETS:
        raise RuntimeError(f"{DATASET} must stay in APPEND_ONLY_DATASETS")
    rows = list(records)
    if not rows:
        raise ValueError("decision_log write refuses an empty batch")
    sessions = {row.get("session_date") for row in rows}
    if len(sessions) != 1 or None in sessions:
        raise ValueError(
            f"decision_log batch mixes session_date values: {sorted(sessions, key=str)}"
        )
    jobs = {row.get("job") for row in rows}
    if jobs != {job}:
        raise ValueError(
            f"decision_log batch job={jobs!r} does not match write job={job!r}"
        )
    return landing.write_clean(
        DATASET, next(iter(sessions)), rows, job=job, data_root=data_root
    )


def write_decision(
    *,
    data_root: str | os.PathLike[str] | None = None,
    **fields: Any,
) -> Path:
    """Build one record and append it. ``fields`` are :func:`decision_record` kwargs."""
    rec = decision_record(**fields)
    return write_decisions([rec], job=rec["job"], data_root=data_root)


def read_decisions(
    dt: date | str,
    *,
    data_root: str | os.PathLike[str] | None = None,
) -> Any:
    """Every decision row for one session as a schema-validated table.

    Reads the whole partition: the dataset is append-only event history, so
    as-of last-file-wins would hide earlier events (``catalog.read_asof``
    refuses it). ``inputs`` / ``signals`` / ``versions`` stay JSON strings;
    decoding is the consumer's call.
    """
    day = dt.isoformat() if isinstance(dt, date) else str(dt)
    return catalog.read_partition(DATASET, date.fromisoformat(day), data_root=data_root)
