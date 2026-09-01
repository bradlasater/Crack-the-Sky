"""Columnar flat-file filtering, local reuse, and exact integer parsing.

The bug that motivated the rewrite: `int(float(v))` on a nanosecond epoch.
Those need ~61 bits and float64 carries a 53-bit mantissa, so the low ~8 bits
were rounded away -- measured at 75% of every landed trade timestamp, off by
up to 128ns. Bar timestamps survived only because they are multiples of 60e9
and so carry 11 trailing zero bits.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pyarrow as pa
import pytest

from ingest import schemas
from ingest.common import landing
from ingest.jobs.flatfile_pull import (
    CLEAN_DATASET,
    _filter_file,
    _filter_table,
    _int_or_none,
    reuse_local,
)

TRADES_CSV = (
    "ticker,conditions,correction,exchange,price,sip_timestamp,size\n"
    # a real ns epoch: 61 bits, not representable in float64
    "O:SPY260918C00770000,209,0,312,6.87,1787923996366000000,1\n"
    "O:SPXW260918P07600000,232,0,302,41.5,1787924018759000001,2\n"
    "O:VIX260916C00020000,209,0,312,1.05,1787935542209999999,3\n"
    "O:VIXW260902P00016000,209,0,312,0.90,1787945043350000003,4\n"
    # foreign roots must not survive the filter
    "O:SPXL260918C00250000,209,0,312,1.00,1787945043350000000,5\n"
    "O:VIXY260918C00020000,209,0,312,1.00,1787945043350000000,6\n"
    # empty numerics must become null, not zero
    "O:SPY260918P00700000,,,,,,\n"
)


# ---------------------------------------------------------------------------
# Integer parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    ["1787923996366000000", "1787924018759000001", "1787935542209999999",
     "9223372036854775807"],
)
def test_nanosecond_epochs_survive_parsing(raw: str) -> None:
    """int(float(v)) rounded these; int(v) does not."""
    assert _int_or_none(raw) == int(raw)
    assert _int_or_none(raw) != int(float(raw)) or int(float(raw)) == int(raw)


def test_float_round_trip_really_does_corrupt() -> None:
    """Pins why the fix is needed, so nobody 'simplifies' it back."""
    v = "1787923996366000000"
    assert int(float(v)) == 1787923996366000128
    assert _int_or_none(v) == 1787923996366000000


def test_empty_and_none_stay_null() -> None:
    assert _int_or_none("") is None
    assert _int_or_none(None) is None


def test_fractional_strings_still_parse() -> None:
    assert _int_or_none("1.0") == 1


# ---------------------------------------------------------------------------
# Columnar filter == row filter
# ---------------------------------------------------------------------------

def _write_csv(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    with gzip.open(p, "wt", newline="", encoding="utf-8") as fh:
        fh.write(body)
    return p


def test_columnar_and_row_paths_agree(tmp_path: Path) -> None:
    """The rewrite must be a speedup, not a change in output."""
    p = _write_csv(tmp_path, "trades.csv.gz", TRADES_CSV)
    recs, rows_in, rows_kept = _filter_file(p, "trades_v1", None)
    table, rows_in2, rows_kept2 = _filter_table(p, "trades_v1")
    assert (rows_in, rows_kept) == (rows_in2, rows_kept2)

    schema = schemas.SCHEMAS[CLEAN_DATASET["trades_v1"]]
    expected = pa.Table.from_pylist(
        [{f.name: r.get(f.name) for f in schema} for r in recs], schema=schema
    )
    assert expected.equals(table)


def test_filter_keeps_the_allowlist_and_drops_lookalikes(tmp_path: Path) -> None:
    p = _write_csv(tmp_path, "trades.csv.gz", TRADES_CSV)
    table, rows_in, rows_kept = _filter_table(p, "trades_v1")
    assert rows_in == 7
    tickers = table.column("ticker").to_pylist()
    assert rows_kept == len(tickers) == 5
    assert "O:SPXL260918C00250000" not in tickers   # Direxion 3x ETF
    assert "O:VIXY260918C00020000" not in tickers   # ProShares VIX ETF
    assert "O:VIX260916C00020000" in tickers
    assert "O:VIXW260902P00016000" in tickers


def test_timestamps_land_exactly(tmp_path: Path) -> None:
    p = _write_csv(tmp_path, "trades.csv.gz", TRADES_CSV)
    table, _, _ = _filter_table(p, "trades_v1")
    got = table.column("sip_timestamp_ns").to_pylist()
    assert 1787923996366000000 in got
    assert 1787924018759000001 in got   # would round to ...000000
    assert 1787935542209999999 in got   # would round to ...210000000


def test_empty_numerics_become_null_not_zero(tmp_path: Path) -> None:
    p = _write_csv(tmp_path, "trades.csv.gz", TRADES_CSV)
    table, _, _ = _filter_table(p, "trades_v1")
    row = {k: v[-1] for k, v in table.to_pydict().items()}
    assert row["ticker"] == "O:SPY260918P00700000"
    assert row["price"] is None and row["size"] is None
    assert row["sip_timestamp_ns"] is None


def test_columns_absent_from_the_file_are_null(tmp_path: Path) -> None:
    """trades_v1 has no trade_id / participant_timestamp; they must not error."""
    p = _write_csv(tmp_path, "trades.csv.gz", TRADES_CSV)
    table, _, _ = _filter_table(p, "trades_v1")
    assert set(table.column("trade_id").to_pylist()) == {None}
    assert set(table.column("participant_timestamp_ns").to_pylist()) == {None}
    assert set(table.column("src").to_pylist()) == {"flatfile"}


def test_output_matches_the_clean_schema(tmp_path: Path) -> None:
    p = _write_csv(tmp_path, "trades.csv.gz", TRADES_CSV)
    table, _, _ = _filter_table(p, "trades_v1")
    assert table.schema.equals(schemas.SCHEMAS["option_trades"])


# ---------------------------------------------------------------------------
# Local reuse
# ---------------------------------------------------------------------------

def _manifest(tmp_path: Path, dataset: str, day: str, md5: str) -> None:
    import json
    landing.meta_path("flatfile_manifest.json", data_root=tmp_path).write_text(
        json.dumps([{"dataset": dataset, "date": day, "md5": md5,
                     "bytes": 1, "rows_in": 1, "rows_kept": 1}])
    )


def test_reuse_when_the_md5_matches(tmp_path: Path) -> None:
    import hashlib
    from datetime import date

    dest = _write_csv(tmp_path, "x.csv.gz", TRADES_CSV)
    md5 = hashlib.md5(dest.read_bytes()).hexdigest()
    _manifest(tmp_path, "trades_v1", "2026-08-28", md5)
    got = reuse_local(dest, tmp_path, "trades_v1", date(2026, 8, 28))
    assert got is not None and got[1] == md5


def test_refetch_when_the_local_copy_is_wrong(tmp_path: Path) -> None:
    """A truncated or half-written file must not be silently trusted."""
    from datetime import date

    dest = _write_csv(tmp_path, "x.csv.gz", TRADES_CSV)
    _manifest(tmp_path, "trades_v1", "2026-08-28", "0" * 32)
    assert reuse_local(dest, tmp_path, "trades_v1", date(2026, 8, 28)) is None


def test_no_reuse_without_a_manifest_entry(tmp_path: Path) -> None:
    from datetime import date

    dest = _write_csv(tmp_path, "x.csv.gz", TRADES_CSV)
    assert reuse_local(dest, tmp_path, "trades_v1", date(2026, 8, 28)) is None


def test_no_reuse_when_the_file_is_absent(tmp_path: Path) -> None:
    from datetime import date

    _manifest(tmp_path, "trades_v1", "2026-08-28", "0" * 32)
    assert reuse_local(tmp_path / "missing.gz", tmp_path, "trades_v1",
                       date(2026, 8, 28)) is None


# ---------------------------------------------------------------------------
# Replace
# ---------------------------------------------------------------------------

def test_quarantine_prior_moves_not_deletes(tmp_path: Path) -> None:
    """Re-filtering must not leave two files to be double-counted."""
    from datetime import date

    rows = [{f.name: None for f in schemas.SCHEMAS["option_day_bars"]}]
    for _ in range(2):
        landing.write_clean("option_day_bars", date(2026, 8, 28), rows,
                            job="flatfile_pull", data_root=tmp_path)
    part = tmp_path / "clean" / "option_day_bars" / "dt=2026-08-28"
    assert len(list(part.glob("*.parquet"))) == 2

    moved = landing.quarantine_prior("option_day_bars", date(2026, 8, 28),
                                     "flatfile_pull", tmp_path)
    assert len(moved) == 2
    assert not list(part.glob("*.parquet"))
    assert all(m.is_file() for m in moved), "old output must remain recoverable"


def test_quarantine_leaves_other_jobs_alone(tmp_path: Path) -> None:
    """reconcile also writes option_minute_bars; only our own output moves."""
    from datetime import date

    rows = [{f.name: None for f in schemas.SCHEMAS["option_minute_bars"]}]
    landing.write_clean("option_minute_bars", date(2026, 8, 28), rows,
                        job="flatfile_pull", data_root=tmp_path)
    landing.write_clean("option_minute_bars", date(2026, 8, 28), rows,
                        job="reconcile", data_root=tmp_path)
    landing.quarantine_prior("option_minute_bars", date(2026, 8, 28),
                             "flatfile_pull", tmp_path)
    left = [p.name for p in
            (tmp_path / "clean" / "option_minute_bars" / "dt=2026-08-28").glob("*.parquet")]
    assert len(left) == 1 and left[0].startswith("reconcile-")
