"""Reconcile WS-captured minute bars against the flat-file truth for a date.

Compares the WS-sourced capture (``raw/option_minute_bars_ws/dt=<date>/``
JSONL, falling back to any clean ``option_minute_bars`` rows with
``src='ws'``) against the flat-file-sourced clean partition
(``clean/option_minute_bars/dt=<date>/`` rows with ``src='flatfile'``):
row counts, distinct tickers and total volume are logged as a delta summary.

The flat file always wins: the date's clean ``option_minute_bars`` partition
is rewritten to contain exactly the flat-file rows (``src='flatfile'``).

Missing flat-file data for the date is not an error: the job logs a clear
message and exits 0 (e.g. S3 creds not yet fixed, or flatfile_pull skipped).

Default ``--date`` is the previous trading day (11:30 Tue-Sat cron targets
yesterday); pass ``--date`` explicitly to reconcile an older day.
"""

from __future__ import annotations

import gzip
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any

from ingest.common import landing, market_gate
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs.flatfile_pull import previous_trading_day
from ingest.jobs.ws_minute_bars import DATASET as WS_RAW_DATASET
from ingest.jobs.ws_minute_bars import parse_events

JOB = "reconcile"
CLEAN_DATASET = "option_minute_bars"


def _ws_raw_stats(data_root: Path, d: date) -> dict[str, Any] | None:
    """Stats from the raw WS JSONL capture for ``d`` (None when absent)."""
    raw_dir = Path(data_root) / "raw" / WS_RAW_DATASET / f"dt={d.isoformat()}"
    if not raw_dir.is_dir():
        return None
    files = sorted(raw_dir.glob("*.jsonl")) + sorted(raw_dir.glob("*.jsonl.gz"))
    if not files:
        return None
    rows = 0
    tickers: set[str] = set()
    volume = 0.0
    for path in files:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[arg-type]
            for line in fh:
                for rec in parse_events(line):
                    rows += 1
                    if rec.get("sym"):
                        tickers.add(rec["sym"])
                    volume += rec.get("v") or 0
    return {"rows": rows, "tickers": len(tickers), "volume": volume}


def _read_partition(data_root: Path, d: date) -> list[dict[str, Any]]:
    """Read every parquet file of the clean option_minute_bars partition."""
    if landing.schemas.pa is None:  # pragma: no cover - pyarrow-less host
        raise ImportError("pyarrow is required for reconcile; pip install -r requirements.txt")
    import pyarrow.parquet as pq

    part_dir = Path(data_root) / "clean" / CLEAN_DATASET / f"dt={d.isoformat()}"
    records: list[dict[str, Any]] = []
    for path in sorted(part_dir.glob("*.parquet")):
        records.extend(pq.read_table(path).to_pylist())
    return records


def _stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Count rows / distinct tickers / summed volume for clean records."""
    return {
        "rows": len(records),
        "tickers": len({r.get("ticker") for r in records if r.get("ticker")}),
        "volume": sum(r.get("volume") or 0 for r in records),
    }


def _main(args: Any, settings: Settings, logger: JsonlLogger) -> dict[str, Any]:
    d = date.fromisoformat(args.date)  # always set: main() injects the default
    data_root = Path(settings.data_root)

    partition = _read_partition(data_root, d)
    ff_records = [r for r in partition if r.get("src") == "flatfile"]
    ws_clean = [r for r in partition if r.get("src") == "ws"]

    if not ff_records:
        msg = (f"no flatfile-sourced option_minute_bars for {d.isoformat()} "
               "(run flatfile_pull first / check S3 creds); nothing to reconcile")
        logger.log("reconcile_skipped", date=d.isoformat(), reason=msg)
        print(msg)
        return {"rows": 0, "reconciled": False}

    ws_stats = _ws_raw_stats(data_root, d)
    if ws_stats is None and ws_clean:
        s = _stats(ws_clean)
        ws_stats = {"rows": s["rows"], "tickers": s["tickers"], "volume": s["volume"]}
    ff_stats = _stats(ff_records)

    summary = {
        "date": d.isoformat(),
        "ws_rows": ws_stats["rows"] if ws_stats else 0,
        "ws_tickers": ws_stats["tickers"] if ws_stats else 0,
        "ws_volume": round(ws_stats["volume"], 4) if ws_stats else 0,
        "flatfile_rows": ff_stats["rows"],
        "flatfile_tickers": ff_stats["tickers"],
        "flatfile_volume": round(ff_stats["volume"], 4),
    }
    summary["delta_rows"] = summary["flatfile_rows"] - summary["ws_rows"]
    summary["delta_volume"] = round(summary["flatfile_volume"] - summary["ws_volume"], 4)
    logger.log("reconcile_delta", **summary)
    if args.dry_run:
        logger.log("reconcile_dry_run", date=d.isoformat())
        return {"rows": len(ff_records), "reconciled": False, **summary}

    # Flat file always wins: rewrite the partition with only flatfile rows.
    part_dir = data_root / "clean" / CLEAN_DATASET / f"dt={d.isoformat()}"
    if part_dir.is_dir():
        shutil.rmtree(part_dir)
    out = landing.write_clean(CLEAN_DATASET, d, ff_records, job=JOB, data_root=data_root)
    logger.log("reconcile_overwritten", date=d.isoformat(), path=str(out),
               rows=len(ff_records))
    return {"rows": len(ff_records), "reconciled": True, **summary}


def main(argv: list[str] | None = None) -> int:
    """Entry point; defaults --date to the previous trading day, then run_job."""
    argv = list(argv) if argv is not None else sys.argv[1:]
    if "--date" not in argv:
        prev = previous_trading_day(market_gate.today_et())
        argv += ["--date", prev.isoformat()]
    return run_job(JOB, _main, argv)  # run_job exits; return is for tests


if __name__ == "__main__":
    sys.exit(main())
