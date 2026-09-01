"""Catalog: schema-strict reads and as-of (last file at or before asof_ns)."""

from __future__ import annotations

from datetime import date

import pytest

from marketdata.catalog import (
    AsOfError,
    CatalogError,
    SchemaError,
    list_partitions,
    read_asof,
    read_partition,
)
from tests.marketdata.conftest import (
    partition_path,
    snapshot_row,
    write_records,
)

DT = date(2026, 8, 28)


def test_list_partitions(tmp_path) -> None:
    write_records(
        partition_path(tmp_path, "option_snapshots", DT, "snapshot_sweep-SPY-1000.parquet"),
        "option_snapshots",
        [snapshot_row("O:SPY260831C00420000")],
    )
    assert list_partitions("option_snapshots", data_root=tmp_path) == [DT]


def test_whole_partition_snapshots_is_an_error(tmp_path) -> None:
    write_records(
        partition_path(tmp_path, "option_snapshots", DT, "snapshot_sweep-SPY-1000.parquet"),
        "option_snapshots",
        [snapshot_row("O:SPY260831C00420000")],
    )
    with pytest.raises(CatalogError, match="read_asof"):
        read_partition("option_snapshots", DT, data_root=tmp_path)


def test_asof_takes_last_at_or_before(tmp_path) -> None:
    early = snapshot_row("O:SPY260831C00420000")
    early["day_close"] = 1.0
    late = snapshot_row("O:SPY260831C00420000")
    late["day_close"] = 2.0
    write_records(
        partition_path(tmp_path, "option_snapshots", DT, "snapshot_sweep-SPY-1000.parquet"),
        "option_snapshots",
        [early],
    )
    write_records(
        partition_path(tmp_path, "option_snapshots", DT, "snapshot_sweep-SPY-2000.parquet"),
        "option_snapshots",
        [late],
    )
    mid = read_asof("option_snapshots", DT, asof_ns=1500 * 1_000_000, data_root=tmp_path)
    assert mid.num_rows == 1
    assert mid["day_close"].to_pylist()[0] == pytest.approx(1.0)
    last = read_asof("option_snapshots", DT, asof_ns=2000 * 1_000_000, data_root=tmp_path)
    assert last["day_close"].to_pylist()[0] == pytest.approx(2.0)
    latest = read_asof("option_snapshots", DT, asof_ns=None, data_root=tmp_path)
    assert latest["day_close"].to_pylist()[0] == pytest.approx(2.0)


def test_asof_before_first_file_fails(tmp_path) -> None:
    write_records(
        partition_path(tmp_path, "option_snapshots", DT, "snapshot_sweep-SPY-1000.parquet"),
        "option_snapshots",
        [snapshot_row("O:SPY260831C00420000")],
    )
    with pytest.raises(AsOfError, match="at or before"):
        read_asof("option_snapshots", DT, asof_ns=500 * 1_000_000, data_root=tmp_path)


def test_extra_column_is_error(tmp_path) -> None:
    path = partition_path(tmp_path, "option_snapshots", DT, "snapshot_sweep-SPY-1000.parquet")
    write_records(
        path,
        "option_snapshots",
        [snapshot_row("O:SPY260831C00420000")],
        extra={"bonus": [1.0]},
    )
    with pytest.raises(SchemaError, match="extra columns"):
        read_asof("option_snapshots", DT, data_root=tmp_path)


def test_missing_column_is_error(tmp_path) -> None:
    path = partition_path(tmp_path, "option_snapshots", DT, "snapshot_sweep-SPY-1000.parquet")
    write_records(
        path,
        "option_snapshots",
        [snapshot_row("O:SPY260831C00420000")],
        drop="ticker",
    )
    with pytest.raises(SchemaError, match="missing columns"):
        read_asof("option_snapshots", DT, data_root=tmp_path)


def test_read_partition_bars(tmp_path) -> None:
    from ingest.schemas import SCHEMAS

    dt = date(2026, 8, 28)
    schema = SCHEMAS["option_minute_bars"]
    rec = {f.name: None for f in schema}
    rec.update(
        {
            "ticker": "O:SPY260831C00420000",
            "window_start_ns": 1,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
            "src": "flatfile",
        }
    )
    write_records(
        partition_path(tmp_path, "option_minute_bars", dt, "flatfile_pull-1000.parquet"),
        "option_minute_bars",
        [rec],
    )
    table = read_partition("option_minute_bars", dt, data_root=tmp_path)
    assert table.num_rows == 1


def test_unknown_dataset() -> None:
    with pytest.raises(CatalogError, match="unknown dataset"):
        list_partitions("not_a_dataset")
