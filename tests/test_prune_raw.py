"""Tests for scripts/prune_raw.sh -- the only script here that deletes data.

Retention bugs are silent until the data is already gone, so the shapes it
walks are asserted directly against a real tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "prune_raw.sh"

DAY = 86400


def _age(path: Path, days: float) -> None:
    """Backdate a directory's mtime; retention is measured from it."""
    when = time.time() - days * DAY
    os.utime(path, (when, when))


def _run(data_root: Path, *args: str, **env: str) -> list[dict]:
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, check=True,
        env={**os.environ, "DATA_ROOT": str(data_root), **env},
    )
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            out.append(json.loads(line))
    return out


def _pruned_paths(events: list[dict]) -> set[str]:
    return {
        e["path"] for e in events
        if e.get("msg") in ("pruned", "would_prune") and "path" in e
    }


@pytest.fixture()
def warehouse(tmp_path: Path) -> Path:
    (tmp_path / "_meta").mkdir(parents=True)
    (tmp_path / "_meta" / "flatfile_manifest.json").write_text("[]", encoding="utf-8")
    return tmp_path


def _quarantine_batch(root: Path, rel: str, days_old: float) -> Path:
    batch = root / "_quarantine" / rel
    batch.mkdir(parents=True)
    (batch / "part.parquet").write_bytes(b"x" * 1024)
    _age(batch, days_old)
    return batch


# ---------------------------------------------------------------------------
# Quarantine: age the batch, never the bucket above it
# ---------------------------------------------------------------------------

def test_prunes_old_batches_in_both_layouts(warehouse: Path) -> None:
    """The date sits at a different depth in each layout."""
    a = _quarantine_batch(warehouse, "pre-root-filter/dt=2026-01-02/option_trades", 90)
    b = _quarantine_batch(warehouse, "refilter/option_day_bars/dt=2023-04-03", 90)
    # Backdate the dt= directories themselves, which is what is aged.
    _age(a.parent, 90)
    _age(b, 90)

    pruned = _pruned_paths(_run(warehouse))
    assert str(a.parent) in pruned      # pre-root-filter/dt=<date>
    assert str(b) in pruned             # refilter/<dataset>/dt=<date>


def test_recent_quarantine_survives_an_old_bucket(warehouse: Path) -> None:
    """The regression: a stale bucket mtime must not condemn fresh batches.

    A directory's mtime does not change when files land below an existing
    child, so refilter/ can look untouched while current output arrives under
    refilter/<dataset>/. Pruning the bucket wholesale would delete the last
    30 days of quarantined data -- the copy someone would actually reach for.
    """
    old = _quarantine_batch(warehouse, "refilter/option_trades/dt=2023-01-03", 90)
    fresh = _quarantine_batch(warehouse, "refilter/option_trades/dt=2026-09-01", 1)
    _age(warehouse / "_quarantine" / "refilter", 400)  # stale bucket mtime

    pruned = _pruned_paths(_run(warehouse))
    assert str(old) in pruned
    assert str(fresh) not in pruned
    assert str(warehouse / "_quarantine" / "refilter") not in pruned


def test_dry_run_deletes_nothing(warehouse: Path) -> None:
    batch = _quarantine_batch(warehouse, "refilter/option_trades/dt=2023-01-03", 90)
    _run(warehouse)
    assert batch.exists()


def test_apply_deletes_only_the_aged_batch(warehouse: Path) -> None:
    old = _quarantine_batch(warehouse, "refilter/option_trades/dt=2023-01-03", 90)
    fresh = _quarantine_batch(warehouse, "refilter/option_trades/dt=2026-09-01", 1)
    _run(warehouse, "--apply")
    assert not old.exists()
    assert fresh.exists()


def test_apply_tidies_emptied_parents_but_keeps_the_root(warehouse: Path) -> None:
    _quarantine_batch(warehouse, "refilter/option_trades/dt=2023-01-03", 90)
    _run(warehouse, "--apply")
    assert not (warehouse / "_quarantine" / "refilter" / "option_trades").exists()
    assert (warehouse / "_quarantine").exists()


def test_quarantine_retention_is_configurable(warehouse: Path) -> None:
    batch = _quarantine_batch(warehouse, "refilter/option_trades/dt=2026-08-01", 45)
    assert str(batch) not in _pruned_paths(_run(warehouse, QUARANTINE_RETAIN_DAYS="60"))
    assert str(batch) in _pruned_paths(_run(warehouse, QUARANTINE_RETAIN_DAYS="30"))


# ---------------------------------------------------------------------------
# Flat files: opt-in, and only what the manifest says parsed
# ---------------------------------------------------------------------------

def _flatfile_day(root: Path, dataset: str, day: str) -> Path:
    part = root / "raw" / "flatfiles" / dataset / f"dt={day}"
    part.mkdir(parents=True)
    (part / f"{day}.csv.gz").write_bytes(b"x" * 1024)
    return part


def _manifest(root: Path, rows: list[dict]) -> None:
    (root / "_meta" / "flatfile_manifest.json").write_text(
        json.dumps(rows), encoding="utf-8"
    )


def test_flatfiles_are_untouched_without_the_flag(warehouse: Path) -> None:
    """The default must stay exactly as it was: the vendor payload is the record."""
    part = _flatfile_day(warehouse, "trades_v1", "2020-01-02")
    _manifest(warehouse, [{"dataset": "trades_v1", "date": "2020-01-02", "rows_kept": 5}])
    assert str(part) not in _pruned_paths(_run(warehouse))


def test_flatfiles_pruned_only_when_the_manifest_shows_rows_kept(warehouse: Path) -> None:
    parsed = _flatfile_day(warehouse, "trades_v1", "2020-01-02")
    unparsed = _flatfile_day(warehouse, "trades_v1", "2020-01-03")
    _manifest(warehouse, [
        {"dataset": "trades_v1", "date": "2020-01-02", "rows_kept": 5},
        {"dataset": "trades_v1", "date": "2020-01-03", "rows_kept": 0},
    ])
    pruned = _pruned_paths(_run(warehouse, "--flatfiles"))
    assert str(parsed) in pruned
    assert str(unparsed) not in pruned


def test_flatfile_datasets_are_aged_independently(warehouse: Path) -> None:
    """One dataset failing to parse must not pin the others."""
    trades = _flatfile_day(warehouse, "trades_v1", "2020-01-02")
    aggs = _flatfile_day(warehouse, "day_aggs_v1", "2020-01-02")
    _manifest(warehouse, [{"dataset": "trades_v1", "date": "2020-01-02", "rows_kept": 5}])
    pruned = _pruned_paths(_run(warehouse, "--flatfiles"))
    assert str(trades) in pruned
    assert str(aggs) not in pruned


# ---------------------------------------------------------------------------
# Destructive-command guards
# ---------------------------------------------------------------------------

def _run_no_check(data_root: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, check=False,
        env={**os.environ, "DATA_ROOT": str(data_root), **env},
    )


@pytest.mark.parametrize(
    "knob", ["RETAIN_DAYS", "QUARANTINE_RETAIN_DAYS", "FLATFILE_RETAIN_DAYS"]
)
@pytest.mark.parametrize("value", ["-30", "abc"])
def test_garbage_retention_knobs_are_rejected(
    warehouse: Path, knob: str, value: str
) -> None:
    """A bad knob must fail before any deletion, not move the cutoff.

    A negative retention would push the cutoff into the future and condemn
    every partition; a non-numeric one would crash date(1) mid-run.
    """
    batch = _quarantine_batch(warehouse, "refilter/option_trades/dt=2023-01-03", 90)
    proc = _run_no_check(warehouse, "--apply", **{knob: value})
    assert proc.returncode == 2
    assert knob in proc.stderr
    assert batch.exists()


def test_malformed_partition_names_are_never_pruned(warehouse: Path) -> None:
    """underlying_day_bars has no manifest gate, so the dt= name itself is
    the only thing standing between a stray directory and deletion."""
    part = warehouse / "raw" / "underlying_day_bars" / "dt=1"
    part.mkdir(parents=True)
    (part / "x.parquet").write_bytes(b"x")
    assert str(part) not in _pruned_paths(_run(warehouse))
    assert part.exists()
