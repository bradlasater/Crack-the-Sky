"""Catalog: schema-strict reads and as-of (last file at or before asof_ns)."""

from __future__ import annotations

from datetime import date

import pytest

from marketdata.catalog import (
    AsOfError,
    CatalogError,
    SchemaError,
    check_src,
    files_by_underlying,
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


def test_files_by_underlying_picks_newest_per_root(tmp_path) -> None:
    for name in (
        "snapshot_sweep-SPY-1000.parquet",
        "snapshot_sweep-SPY-2000.parquet",
        "snapshot_sweep-I:SPX-1500.parquet",
    ):
        write_records(
            partition_path(tmp_path, "option_snapshots", DT, name),
            "option_snapshots",
            [snapshot_row("O:SPY260831C00420000")],
        )
    by_underlying, unstamped = files_by_underlying("option_snapshots", DT, data_root=tmp_path)
    assert {u: p.name for u, p in by_underlying.items()} == {
        "SPY": "snapshot_sweep-SPY-2000.parquet",
        "I:SPX": "snapshot_sweep-I:SPX-1500.parquet",
    }
    assert unstamped == []


def test_files_by_underlying_buckets_unstamped_and_filters_asof(tmp_path) -> None:
    for name in ("snapshot_sweep-SPY-1000.parquet", "snapshot_sweep-SPY-2000.parquet"):
        write_records(
            partition_path(tmp_path, "option_snapshots", DT, name),
            "option_snapshots",
            [snapshot_row("O:SPY260831C00420000")],
        )
    foreign = partition_path(tmp_path, "option_snapshots", DT, "hand_written.parquet")
    foreign.touch()
    by_underlying, unstamped = files_by_underlying(
        "option_snapshots", DT, data_root=tmp_path, asof_ns=1500 * 1_000_000
    )
    assert [p.name for p in by_underlying.values()] == ["snapshot_sweep-SPY-1000.parquet"]
    assert [p.name for p in unstamped] == ["hand_written.parquet"]


# ---------------------------------------------------------------------------
# option_trades holds two overlapping sources in one partition
# ---------------------------------------------------------------------------

def _trade_row(ticker: str, ts_ns: int, src: str) -> dict:
    return {
        "ticker": ticker, "price": 1.25, "size": 3, "exchange": 300,
        "conditions": None, "correction": 0, "trade_id": None,
        "sequence_number": None, "sip_timestamp_ns": ts_ns,
        "participant_timestamp_ns": None, "src": src,
    }


def _both_sources(tmp_path) -> None:
    write_records(
        partition_path(tmp_path, "option_trades", DT, "flatfile_pull-1000.parquet"),
        "option_trades",
        [_trade_row("O:SPY260831C00420000", 1_000_000_000_000_000_000, "flatfile"),
         _trade_row("O:SPY260831C00420000", 1_000_000_000_100_000_000, "flatfile")],
    )
    write_records(
        partition_path(tmp_path, "option_trades", DT, "trades_watchlist-2000.parquet"),
        "option_trades",
        [_trade_row("O:SPY260831C00420000", 1_000_000_000_000_000_000, "rest")],
    )


def test_option_trades_whole_partition_read_requires_a_source(tmp_path) -> None:
    """Reading both sources returns the same trade twice; make that an error."""
    _both_sources(tmp_path)
    with pytest.raises(CatalogError, match="overlapping sources"):
        read_partition("option_trades", DT, data_root=tmp_path)


def test_option_trades_src_selects_one_source(tmp_path) -> None:
    _both_sources(tmp_path)
    ff = read_partition("option_trades", DT, data_root=tmp_path, src="flatfile")
    rest = read_partition("option_trades", DT, data_root=tmp_path, src="rest")
    assert ff.num_rows == 2
    assert rest.num_rows == 1
    assert set(ff.column("src").to_pylist()) == {"flatfile"}
    assert set(rest.column("src").to_pylist()) == {"rest"}


def test_option_trades_missing_source_is_an_error_not_an_empty_table(tmp_path) -> None:
    """Absence must be loud: a same-day read before flatfile_pull lands."""
    write_records(
        partition_path(tmp_path, "option_trades", DT, "trades_watchlist-2000.parquet"),
        "option_trades",
        [_trade_row("O:SPY260831C00420000", 1_000_000_000_000_000_000, "rest")],
    )
    with pytest.raises(CatalogError, match="no src='flatfile' rows"):
        read_partition("option_trades", DT, data_root=tmp_path, src="flatfile")


def test_src_on_a_single_source_dataset_is_an_error(tmp_path) -> None:
    """Silently ignoring src would let a typo read the wrong thing forever."""
    write_records(
        partition_path(tmp_path, "option_minute_bars", DT, "flatfile_pull-1000.parquet"),
        "option_minute_bars",
        [{"ticker": "O:SPY260831C00420000", "window_start_ns": 1_000_000_000_000_000_000,
          "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1,
          "transactions": 1, "vwap": 1.0, "src": "flatfile"}],
    )
    with pytest.raises(CatalogError, match="single source"):
        read_partition("option_minute_bars", DT, data_root=tmp_path, src="flatfile")


def test_check_src_rejects_an_unknown_source(tmp_path) -> None:
    with pytest.raises(CatalogError, match="unknown src"):
        check_src("option_trades", "flatfle")


def test_missing_partition_still_enforces_the_source_rule(tmp_path) -> None:
    """The rule must not depend on whether that date happens to have landed.

    ``ingest.jobs.read_partition`` short-circuits on a missing partition, so
    without an explicit check first, option_trades would quietly accept a
    missing src on every date not yet pulled.
    """
    from datetime import date as _date

    from ingest.common.config import Settings
    from ingest.jobs import read_partition as jobs_read_partition

    settings = Settings(massive_api_key="k", data_root=tmp_path,
                        log_root=tmp_path / "logs")
    with pytest.raises(CatalogError, match="overlapping sources"):
        jobs_read_partition(settings, "option_trades", _date(2001, 1, 1))
    with pytest.raises(CatalogError, match="single source"):
        jobs_read_partition(settings, "option_minute_bars", _date(2001, 1, 1), src="rest")
    # A valid pairing on a missing partition is still just "no rows".
    assert jobs_read_partition(settings, "option_trades", _date(2001, 1, 1), src="rest") == []
