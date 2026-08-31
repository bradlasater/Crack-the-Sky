"""Landing-zone writers: raw JSONL/bytes payloads and clean parquet.

Layout under ``DATA_ROOT`` (env var, default ``/data/massive``)::

    raw/{dataset}/dt={YYYY-MM-DD}/{job}-{epoch_ms}.{fmt}[.gz]   (append)
    clean/{dataset}/dt={YYYY-MM-DD}/{job}-{epoch_ms}.parquet
    _meta/{name}

Clean writes are projected onto ``ingest.schemas.SCHEMAS[dataset]``: extra
record keys are dropped, missing keys become null. PyArrow is only required
for :func:`write_clean`; raw writers work without it.
"""

from __future__ import annotations

import gzip
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
    records: Iterable[dict[str, Any]] | bytes | str,
    job: str,
    fmt: str = "jsonl",
    data_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Append raw payload(s) to the raw landing zone; returns the file path.

    ``records`` may be an iterable of dicts (written as JSON Lines), a str, or
    bytes. If ``fmt`` ends with ``.gz`` the file is gzip-compressed. Appends
    when the file already exists (same epoch-ms or rerun).
    """
    day = dt.isoformat() if isinstance(dt, date) else str(dt)
    out_dir = _data_root(data_root) / "raw" / dataset / f"dt={day}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{job}-{_epoch_ms()}.{fmt}"

    if isinstance(records, bytes):
        payload = records
    elif isinstance(records, str):
        payload = records.encode("utf-8")
    else:
        payload = "".join(json.dumps(r, default=str) + "\n" for r in records).encode("utf-8")

    if fmt.endswith(".gz"):
        with gzip.open(path, "ab") as fh:
            fh.write(payload)
    else:
        with open(path, "ab") as fh:
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


def meta_path(name: str, data_root: str | os.PathLike[str] | None = None) -> Path:
    """Return ``{DATA_ROOT}/_meta/{name}``, creating the _meta directory."""
    base = _data_root(data_root) / "_meta"
    base.mkdir(parents=True, exist_ok=True)
    return base / name
