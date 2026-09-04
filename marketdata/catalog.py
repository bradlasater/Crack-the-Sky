"""Partition listing, schema-strict parquet reads, and per-underlying file selection.

Ingest writes many files per day for snapshots and contracts. Reading a whole
partition double-counts. As-of takes the last file **at or before** ``asof_ns``
per underlying, using the epoch-ms stamp in the filename.

This module is the single home for clean-partition IO: ``ingest.jobs``
delegates its ``Settings``-based, list-of-dicts readers here rather than
re-walking ``dt=`` partitions itself.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ingest.schemas import SCHEMAS

# Datasets written more than once a day: whole-partition reads double-count.
ASOF_DATASETS: frozenset[str] = frozenset(
    {"option_snapshots", "contracts", "contracts_expired", "forwards"}
)

# Datasets a partition can hold from more than one source, where the sources
# overlap. ``option_trades`` is the case: ``flatfile_pull`` lands the
# authoritative T+1 record for the day, and ``trades_watchlist`` lands a
# same-day REST approximation of the same trades. Reading the partition whole
# returns both, which double-counts every trade the two agree on -- measured
# at ~74% of same-day REST rows on 2026-09-02, with the remainder differing
# only in timestamp precision (flat files are millisecond-aligned, REST is
# not), so the true overlap is higher still.
#
# There is no defensible default here: "flatfile" is authoritative but does
# not exist until T+1, and "rest" is the only same-day record but is partial.
# So the choice is the caller's, and not making it is an error.
MULTI_SOURCE_DATASETS: frozenset[str] = frozenset({"option_trades"})

# Values of the ``src`` column, which is what the choice actually selects.
SRC_FLATFILE, SRC_REST = "flatfile", "rest"


class CatalogError(ValueError):
    """Fail-loud catalog error (unknown dataset, missing partition, ...)."""


class SchemaError(CatalogError):
    """Parquet columns do not match ``ingest.schemas.SCHEMAS[dataset]``."""


class AsOfError(CatalogError):
    """No file in the partition is at or before the requested instant."""


def _data_root(data_root: str | os.PathLike[str] | None = None) -> Path:
    if data_root is not None:
        return Path(data_root)
    return Path(os.environ.get("DATA_ROOT", "/data/massive"))


def _clean_dir(data_root: Path, dataset: str) -> Path:
    return data_root / "clean" / dataset


def _schema(dataset: str) -> Any:
    if dataset not in SCHEMAS:
        raise CatalogError(f"unknown dataset {dataset!r}; known: {sorted(SCHEMAS)}")
    return SCHEMAS[dataset]


def list_partitions(
    dataset: str,
    data_root: str | os.PathLike[str] | None = None,
) -> list[date]:
    """``dt=YYYY-MM-DD`` partition dates present under ``clean/{dataset}``."""
    _schema(dataset)
    root = _clean_dir(_data_root(data_root), dataset)
    out: list[date] = []
    if not root.is_dir():
        return out
    for child in root.iterdir():
        if child.is_dir() and child.name.startswith("dt="):
            try:
                out.append(date.fromisoformat(child.name[3:]))
            except ValueError as exc:
                raise CatalogError(
                    f"malformed partition directory {child.name} under {root}"
                ) from exc
    return sorted(out)


def _parse_stamp(path: Path) -> tuple[int | None, str | None]:
    """``(epoch_ms, underlying)`` from ``{job}-{underlying}-{epoch_ms}.parquet``.

    Files named ``{job}-{epoch_ms}.parquet`` return ``(epoch_ms, None)``.
    """
    parts = path.stem.rsplit("-", 2)
    if len(parts) == 3:
        try:
            return int(parts[2]), parts[1]
        except ValueError:
            pass
    parts2 = path.stem.rsplit("-", 1)
    if len(parts2) == 2:
        try:
            return int(parts2[1]), None
        except ValueError:
            pass
    return None, None


def _partition_dir(data_root: Path, dataset: str, dt: date) -> Path:
    return _clean_dir(data_root, dataset) / f"dt={dt.isoformat()}"


def _parquet_files(part: Path) -> list[Path]:
    if not part.is_dir():
        return []
    return sorted(part.glob("*.parquet"))


def files_by_underlying(
    dataset: str,
    dt: date,
    data_root: str | os.PathLike[str] | None = None,
    asof_ns: int | None = None,
) -> tuple[dict[str | None, Path], list[Path]]:
    """Newest stamped parquet per underlying in one clean partition.

    Returns ``(by_underlying, unstamped)``: the highest-epoch_ms file per
    underlying label (key ``None`` for ``{job}-{epoch_ms}`` names), and every
    file whose name carries no epoch-ms stamp. ``asof_ns`` keeps only files
    stamped at or before that instant (ns epoch). Fail-loud callers
    (:func:`read_asof`) treat ``unstamped`` as an error; tolerant callers
    (``ingest.jobs``) read those files separately.
    """
    _schema(dataset)
    part = _partition_dir(_data_root(data_root), dataset, dt)
    files = _parquet_files(part)
    if not files:
        raise CatalogError(f"no parquet in {part}")
    latest: dict[str | None, tuple[int, Path]] = {}
    unstamped: list[Path] = []
    for path in files:
        epoch_ms, underlying = _parse_stamp(path)
        if epoch_ms is None:
            unstamped.append(path)
            continue
        if asof_ns is not None and epoch_ms * 1_000_000 > asof_ns:
            continue
        prev = latest.get(underlying)
        if prev is None or epoch_ms > prev[0]:
            latest[underlying] = (epoch_ms, path)
    return {u: p for u, (_ms, p) in latest.items()}, unstamped


def validate_arrow_schema(table: pa.Table, dataset: str) -> None:
    """Extra or missing columns are errors. Type mismatches are errors."""
    expected = _schema(dataset)
    got_names = list(table.schema.names)
    exp_names = list(expected.names)
    extra = [n for n in got_names if n not in exp_names]
    missing = [n for n in exp_names if n not in got_names]
    if extra or missing:
        raise SchemaError(f"{dataset}: extra columns {extra}, missing columns {missing}")
    for field in expected:
        got = table.schema.field(field.name)
        if got.type != field.type:
            raise SchemaError(
                f"{dataset}: column {field.name!r} has type {got.type}, expected {field.type}"
            )


def _read_file(path: Path, dataset: str) -> pa.Table:
    table = pq.read_table(path)
    validate_arrow_schema(table, dataset)
    # Reorder to the canonical schema; names already match.
    return table.select(list(_schema(dataset).names))


def read_partition(
    dataset: str,
    dt: date,
    data_root: str | os.PathLike[str] | None = None,
    src: str | None = None,
) -> pa.Table:
    """Read every parquet file of one clean partition, schema-validated.

    Do not use this for :data:`ASOF_DATASETS` — those double-count. Use
    :func:`read_asof` instead.

    For :data:`MULTI_SOURCE_DATASETS`, ``src`` is required and selects one
    source ("flatfile" or "rest"); omitting it raises rather than silently
    returning an overlapping union of the two.
    """
    _schema(dataset)
    if dataset in ASOF_DATASETS:
        raise CatalogError(
            f"{dataset} is written many times a day; use read_asof "
            "(a whole-partition read double-counts)"
        )
    if dataset in MULTI_SOURCE_DATASETS:
        if src is None:
            raise CatalogError(
                f"{dataset} partitions hold overlapping sources; pass "
                f"src={SRC_FLATFILE!r} for the authoritative T+1 record or "
                f"src={SRC_REST!r} for the same-day REST capture "
                "(reading both double-counts)"
            )
    elif src is not None:
        raise CatalogError(f"{dataset} has a single source; drop src={src!r}")

    root = _data_root(data_root)
    part = _partition_dir(root, dataset, dt)
    files = _parquet_files(part)
    if not files:
        raise CatalogError(f"no parquet in {part}")
    tables = [_read_file(p, dataset) for p in files]
    table = pa.concat_tables(tables)
    if src is not None:
        table = table.filter(pc.equal(table.column("src"), src))
        if table.num_rows == 0:
            raise CatalogError(f"no src={src!r} rows in {part}")
    return table


def read_asof(
    dataset: str,
    dt: date,
    asof_ns: int | None = None,
    data_root: str | os.PathLike[str] | None = None,
) -> pa.Table:
    """Last file per underlying at or before ``asof_ns`` (ns epoch).

    ``asof_ns=None`` means the latest file per underlying in that partition.
    Absence (no file at or before the instant) is an error, as is any file
    whose name carries no epoch-ms stamp.
    """
    root = _data_root(data_root)
    part = _partition_dir(root, dataset, dt)
    by_underlying, unstamped = files_by_underlying(dataset, dt, root, asof_ns)
    if unstamped:
        raise AsOfError(
            f"cannot as-of {unstamped[0].name}: filename is not "
            "{job}-{underlying}-{epoch_ms}.parquet"
        )
    if not by_underlying:
        raise AsOfError(f"no {dataset} file in {part} at or before asof_ns={asof_ns}")
    tables = [_read_file(p, dataset) for p in sorted(by_underlying.values())]
    return pa.concat_tables(tables)
