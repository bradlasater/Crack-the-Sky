"""Landing-zone writers: raw JSONL/bytes payloads and clean parquet.

Layout under ``DATA_ROOT`` (env var, default ``/data/massive``)::

    raw/{dataset}/dt={YYYY-MM-DD}/{job}-{epoch_ms}.jsonl   (append)
    clean/{dataset}/dt={YYYY-MM-DD}/{job}-{epoch_ms}.parquet
    _meta/{name}

Clean writes are projected onto ``ingest.schemas.SCHEMAS[dataset]``: extra
record keys are dropped, missing keys become null. PyArrow is only required
for :func:`write_clean`; raw writers work without it.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from ingest import schemas


def _data_root(data_root: str | os.PathLike[str] | None = None) -> Path:
    if data_root is not None:
        return Path(data_root)
    return Path(os.environ.get("DATA_ROOT", "/data/massive"))


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def write_raw(
    dataset: str,
    dt: date | str,
    records: Iterable[dict[str, Any]],
    job: str,
    data_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Append records as JSON Lines to the raw landing zone; returns the path.

    Appends when the file already exists (same epoch-ms or rerun).
    """
    day = dt.isoformat() if isinstance(dt, date) else str(dt)
    out_dir = _data_root(data_root) / "raw" / dataset / f"dt={day}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{job}-{_epoch_ms()}.jsonl"
    payload = "".join(json.dumps(r, default=str) + "\n" for r in records).encode("utf-8")
    with open(path, "ab") as fh:
        fh.write(payload)
    return path


def write_raw_text(
    dataset: str,
    dt: date | str,
    text: str | bytes,
    job: str,
    ext: str = "txt",
    data_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Land a vendor payload verbatim; returns the path.

    ``write_raw`` JSON-encodes an iterable of records, which silently turns a
    string into one JSON line per character. Payloads that are already a
    document -- Flex XML, for instance -- need to land byte-for-byte, because
    the raw zone is the record of truth and is never rewritten.
    """
    day = dt.isoformat() if isinstance(dt, date) else str(dt)
    out_dir = _data_root(data_root) / "raw" / dataset / f"dt={day}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{job}-{_epoch_ms()}.{ext.lstrip('.')}"
    payload = text if isinstance(text, bytes) else text.encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(payload)
    return path


def write_clean(
    dataset: str,
    dt: date | str,
    records: Iterable[dict[str, Any]],
    job: str,
    data_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Write records as a schema-projected parquet file; returns the path.

    Records may carry extra keys (dropped) and may omit keys (written null).
    Requires pyarrow; raises ImportError with a clear message otherwise.
    """
    if schemas.pa is None:  # pragma: no cover - only on pyarrow-less hosts
        raise ImportError(
            "pyarrow is required for clean parquet writes; "
            "install it (pip install -r requirements.txt)"
        )
    import pyarrow.parquet as pq

    schema = schemas.SCHEMAS[dataset]
    projected = [
        {field.name: rec.get(field.name) for field in schema} for rec in records
    ]
    table = schemas.pa.Table.from_pylist(projected, schema=schema)

    day = dt.isoformat() if isinstance(dt, date) else str(dt)
    out_dir = _data_root(data_root) / "clean" / dataset / f"dt={day}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{job}-{_epoch_ms()}.parquet"
    pq.write_table(table, path)
    return path


def write_clean_table(
    dataset: str,
    dt: date | str,
    table: Any,
    job: str,
    data_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Write an already-schema-matching pyarrow Table; returns the path.

    ``write_clean`` projects a list of dicts, which means materialising every
    row in Python. Callers that already filtered columnar (flatfile_pull) skip
    that entirely.
    """
    import pyarrow.parquet as pq

    schema = schemas.SCHEMAS[dataset]
    if not table.schema.equals(schema):
        raise ValueError(
            f"table schema does not match SCHEMAS[{dataset!r}]:\n"
            f"  got:      {table.schema}\n  expected: {schema}"
        )
    day = dt.isoformat() if isinstance(dt, date) else str(dt)
    out_dir = _data_root(data_root) / "clean" / dataset / f"dt={day}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{job}-{_epoch_ms()}.parquet"
    pq.write_table(table, path)
    return path


def quarantine_prior(
    dataset: str,
    dt: date | str,
    job: str,
    data_root: str | os.PathLike[str] | None = None,
) -> list[Path]:
    """Move this job's earlier output for a partition aside; returns the paths.

    Re-filtering a partition writes a NEW timestamped file, so the previous
    one would remain and be double-counted by a whole-partition read. Moving
    rather than deleting keeps the old output recoverable under
    ``_quarantine/`` -- the raw payload is still on disk either way, but a
    rename is free and a delete is not reversible.
    """
    day = dt.isoformat() if isinstance(dt, date) else str(dt)
    root = _data_root(data_root)
    part = root / "clean" / dataset / f"dt={day}"
    if not part.is_dir():
        return []
    dest = root / "_quarantine" / "refilter" / dataset / f"dt={day}"
    moved: list[Path] = []
    for path in sorted(part.glob(f"{job}-*.parquet")):
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / path.name
        path.replace(target)
        moved.append(target)
    return moved


def meta_path(name: str, data_root: str | os.PathLike[str] | None = None) -> Path:
    """Return ``{DATA_ROOT}/_meta/{name}``, creating the _meta directory."""
    base = _data_root(data_root) / "_meta"
    base.mkdir(parents=True, exist_ok=True)
    return base / name
