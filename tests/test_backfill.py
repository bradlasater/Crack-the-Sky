"""Tests for scripts/backfill.sh — the flat-file backfill orchestrator.

Two behaviours are pinned here:

* the skip check is rows_kept-aware, matching the index prune_raw.sh builds,
  so a date whose three files parsed to zero rows is re-pulled instead of
  being skipped forever;
* BACKFILL_WORKERS runs dates in parallel, which is only safe because
  flatfile_pull._update_manifest serializes its read-modify-write with flock
  -- the stub job below uses the real _update_manifest, so a broken lock
  shows up here as a corrupt or lossy manifest.

Requires GNU coreutils (xargs -r, df -BG) and bash 4+, like prune_raw.sh.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "backfill.sh"

DATASETS = ("trades_v1", "minute_aggs_v1", "day_aggs_v1")


def _entry(dataset: str, day: str, rows_kept: int) -> dict:
    return {"dataset": dataset, "date": day, "bytes": 1,
            "rows_in": 1, "rows_kept": rows_kept, "md5": "x"}


def _seed_manifest(root: Path, rows: list[dict]) -> None:
    meta = root / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "flatfile_manifest.json").write_text(json.dumps(rows), encoding="utf-8")


@pytest.fixture()
def stub_py(tmp_path: Path) -> Path:
    """A stand-in for `venv/bin/python -m ingest.jobs.flatfile_pull`.

    Records each --date it is asked for, then appends the three manifest
    entries through the real _update_manifest (exercising the flock under
    parallel workers). STUB_ROWS_KEPT controls the rows_kept written;
    STUB_FAIL_DATE makes that date's pull exit 1.
    """
    stub = tmp_path / "stub-python"
    stub.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
d=""
while [ $# -gt 0 ]; do
  case "$1" in
    --date) d="$2"; shift 2 ;;
    *) shift ;;
  esac
done
mkdir -p "$STUB_CALLS_DIR"
echo "$d" >> "$STUB_CALLS_DIR/calls"
if [ -n "${{STUB_FAIL_DATE:-}}" ] && [ "$d" = "$STUB_FAIL_DATE" ]; then
  exit 1
fi
exec {sys.executable} - "$DATA_ROOT" "$d" <<'PYEOF'
import os
import sys
from pathlib import Path

sys.path.insert(0, {str(REPO_ROOT)!r})
from ingest.jobs.flatfile_pull import _update_manifest

root, d = Path(sys.argv[1]), sys.argv[2]
rows_kept = int(os.environ.get("STUB_ROWS_KEPT", "5"))
for ds in ("trades_v1", "minute_aggs_v1", "day_aggs_v1"):
    _update_manifest(root, {{"dataset": ds, "date": d, "bytes": 1,
                            "rows_in": 1, "rows_kept": rows_kept, "md5": "x"}})
PYEOF
""",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _run(
    data_root: Path, start: str, end: str, stub_py: Path, **env: str
) -> subprocess.CompletedProcess[str]:
    calls = data_root / "_calls"
    return subprocess.run(
        ["bash", str(SCRIPT), start, end],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={
            **os.environ,
            "DATA_ROOT": str(data_root),
            "BACKFILL_PY": str(stub_py),
            "STUB_CALLS_DIR": str(calls),
            "BACKFILL_SLEEP_S": "0",
            "MIN_FREE_GB": "0",
            **env,
        },
    )


def _calls(data_root: Path) -> list[str]:
    f = data_root / "_calls" / "calls"
    return f.read_text(encoding="utf-8").split() if f.exists() else []


def test_zero_rows_kept_dates_are_repulled_not_skipped(
    tmp_path: Path, stub_py: Path
) -> None:
    """The old grep-count check treated 3 zero-row entries as done forever."""
    _seed_manifest(tmp_path, [
        *(_entry(ds, "2026-08-24", 5) for ds in DATASETS),   # genuinely done
        *(_entry(ds, "2026-08-25", 0) for ds in DATASETS),   # parsed to nothing
    ])
    proc = _run(tmp_path, "2026-08-24", "2026-08-25", stub_py, BACKFILL_WORKERS="1")
    assert proc.returncode == 0, proc.stderr
    assert _calls(tmp_path) == ["2026-08-25"]


def test_a_partially_landed_date_is_repulled(tmp_path: Path, stub_py: Path) -> None:
    """Done means all three datasets, not any three manifest lines."""
    _seed_manifest(tmp_path, [
        _entry("trades_v1", "2026-08-24", 5),
        _entry("day_aggs_v1", "2026-08-24", 5),
    ])
    proc = _run(tmp_path, "2026-08-24", "2026-08-24", stub_py, BACKFILL_WORKERS="1")
    assert proc.returncode == 0, proc.stderr
    assert _calls(tmp_path) == ["2026-08-24"]


def test_serial_order_is_newest_first_by_default(tmp_path: Path, stub_py: Path) -> None:
    proc = _run(tmp_path, "2026-08-24", "2026-08-26", stub_py, BACKFILL_WORKERS="1")
    assert proc.returncode == 0, proc.stderr
    assert _calls(tmp_path) == ["2026-08-26", "2026-08-25", "2026-08-24"]


def test_oldest_first_order(tmp_path: Path, stub_py: Path) -> None:
    proc = _run(
        tmp_path, "2026-08-24", "2026-08-26", stub_py,
        BACKFILL_WORKERS="1", BACKFILL_ORDER="oldest",
    )
    assert proc.returncode == 0, proc.stderr
    assert _calls(tmp_path) == ["2026-08-24", "2026-08-25", "2026-08-26"]


def test_a_rerun_after_success_pulls_nothing(tmp_path: Path, stub_py: Path) -> None:
    """Resume-safety: entries the stub lands with rows kept are done."""
    args = ("2026-08-24", "2026-08-26", stub_py)
    assert _run(tmp_path, *args, BACKFILL_WORKERS="1").returncode == 0
    assert _run(tmp_path, *args, BACKFILL_WORKERS="1").returncode == 0
    assert len(_calls(tmp_path)) == 3


def test_parallel_workers_pull_every_date_once(tmp_path: Path, stub_py: Path) -> None:
    days = [f"2026-08-{d}" for d in range(24, 32)]
    proc = _run(tmp_path, days[0], days[-1], stub_py, BACKFILL_WORKERS="4")
    assert proc.returncode == 0, proc.stderr
    assert sorted(_calls(tmp_path)) == days
    manifest = json.loads(
        (tmp_path / "_meta" / "flatfile_manifest.json").read_text(encoding="utf-8")
    )
    keys = [(e["dataset"], e["date"]) for e in manifest]
    assert len(keys) == len(set(keys)) == 3 * len(days), (
        "concurrent manifest updates lost or duplicated entries"
    )


def test_a_failed_date_does_not_stop_the_batch(tmp_path: Path, stub_py: Path) -> None:
    proc = _run(
        tmp_path, "2026-08-24", "2026-08-26", stub_py,
        BACKFILL_WORKERS="3", STUB_FAIL_DATE="2026-08-25",
    )
    assert proc.returncode == 0, proc.stderr
    assert sorted(_calls(tmp_path)) == ["2026-08-24", "2026-08-25", "2026-08-26"]
    assert "continuing" in proc.stdout


def test_everything_already_done_pulls_nothing(tmp_path: Path, stub_py: Path) -> None:
    days = ["2026-08-24", "2026-08-25"]
    _seed_manifest(tmp_path, [_entry(ds, d, 5) for d in days for ds in DATASETS])
    proc = _run(tmp_path, days[0], days[-1], stub_py, BACKFILL_WORKERS="4")
    assert proc.returncode == 0, proc.stderr
    assert _calls(tmp_path) == []
