"""Fail-loud validation: foreign roots, mixed underlyings, nulls, empty roots."""

from __future__ import annotations

from datetime import date

import pyarrow as pa

from ingest.schemas import SCHEMAS
from marketdata.validate import FAIL, PASS, main, validate_table, validate_tickers
from tests.marketdata.conftest import (
    partition_path,
    snapshot_row,
    write_records,
)


def test_foreign_roots_fail() -> None:
    checks = validate_tickers(
        ["O:SPY260831C00420000", "O:SPYL260831C00420000"],
        roots=("SPY",),
    )
    purity = next(c for c in checks if c.name == "ticker_purity")
    assert purity.status == FAIL
    assert "SPYL" in purity.detail


def test_spy_only_rejects_spx() -> None:
    """Mixed underlyings in a SPY-only query are a failure."""
    checks = validate_tickers(
        ["O:SPY260831C00420000", "O:SPX260918C07000000"],
        roots=("SPY",),
    )
    purity = next(c for c in checks if c.name == "ticker_purity")
    assert purity.status == FAIL
    assert "SPX" in purity.detail


def test_empty_allowlisted_root_fails() -> None:
    checks = validate_tickers(
        ["O:SPY260831C00420000"],
        roots=("SPY", "SPX", "SPXW"),
    )
    by_name = {c.name: c for c in checks}
    assert by_name["root[SPY]"].status == PASS
    assert by_name["root[SPX]"].status == FAIL
    assert by_name["root[SPXW]"].status == FAIL
    assert "absence is a failure" in by_name["root[SPX]"].detail


def test_required_nulls_fail() -> None:
    schema = SCHEMAS["option_snapshots"]
    rec = snapshot_row("O:SPY260831C00420000")
    rec["ticker"] = None
    table = pa.Table.from_pylist([{f.name: rec.get(f.name) for f in schema}], schema=schema)
    checks = validate_table(table, "option_snapshots", roots=("SPY",))
    req = next(c for c in checks if c.name == "required[ticker]")
    assert req.status == FAIL


def test_empty_table_fails() -> None:
    table = SCHEMAS["option_snapshots"].empty_table()
    checks = validate_table(table, "option_snapshots")
    assert checks[0].status == FAIL
    assert "empty" in checks[0].detail


def test_cli_exit_nonzero_on_foreign_root(tmp_path) -> None:
    dt = date(2026, 8, 28)
    write_records(
        partition_path(tmp_path, "option_snapshots", dt, "snapshot_sweep-SPY-1000.parquet"),
        "option_snapshots",
        [
            snapshot_row("O:SPY260831C00420000"),
            snapshot_row("O:SPYL260831C00420000", underlying="SPYL"),
        ],
    )
    rc = main(
        [
            "--dataset",
            "option_snapshots",
            "--date",
            dt.isoformat(),
            "--roots",
            "SPY",
            "--data-root",
            str(tmp_path),
        ]
    )
    assert rc == 1


def test_cli_ok_on_clean_spy(tmp_path) -> None:
    dt = date(2026, 8, 28)
    write_records(
        partition_path(tmp_path, "option_snapshots", dt, "snapshot_sweep-SPY-1000.parquet"),
        "option_snapshots",
        [snapshot_row("O:SPY260831C00420000")],
    )
    rc = main(
        [
            "--dataset",
            "option_snapshots",
            "--date",
            dt.isoformat(),
            "--roots",
            "SPY",
            "--data-root",
            str(tmp_path),
        ]
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# --roots narrows the allowlist; it must never extend it
# ---------------------------------------------------------------------------

def test_roots_cannot_admit_a_foreign_root() -> None:
    """`--roots SPYL` would otherwise make SPYL pass the purity check."""
    import pytest

    from marketdata.validate import narrow_roots

    with pytest.raises(ValueError, match="not in the catalog allowlist"):
        narrow_roots(("SPYL",))
    with pytest.raises(ValueError, match="not in the catalog allowlist"):
        narrow_roots(("SPY", "SPXL"))


def test_roots_can_narrow() -> None:
    from marketdata.validate import narrow_roots

    assert narrow_roots(("SPY",)) == ("SPY",)
    assert narrow_roots(("spy", " SPXW ")) == ("SPY", "SPXW")


def test_empty_roots_rejected() -> None:
    import pytest

    from marketdata.validate import narrow_roots

    with pytest.raises(ValueError, match="no roots"):
        narrow_roots(())
