"""Throwaway smoke check for core plumbing: market gate + JSONL logging.

Exercises require_trading_day (open day passes, closed day exits 0 quietly,
--force bypasses) and the run-log path used by cli.run_job. Not part of the
test suite; safe to delete once the jobs land.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path

from ingest.common import market_gate
from ingest.common.logging_utils import get_run_logger

HOLIDAYS = [
    {"date": "2026-09-07", "exchange": "NYSE", "name": "Labor Day", "status": "closed"},
    {"date": "2026-11-27", "exchange": "NYSE", "name": "Thanksgiving",
     "status": "early-close", "early_close": True},
]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "_meta").mkdir()
        (root / "_meta" / "holidays.json").write_text(json.dumps(HOLIDAYS))

        # market gate: open weekday passes
        market_gate.require_trading_day(date(2026, 9, 8), data_root=root)
        print("require_trading_day(open weekday) -> passed")

        # closed holiday exits quietly with code 0
        try:
            market_gate.require_trading_day(date(2026, 9, 7), data_root=root)
        except SystemExit as exc:
            assert exc.code == 0, exc.code
            print("require_trading_day(holiday) -> SystemExit(0) as expected")
        else:
            raise AssertionError("holiday was not gated")

        # --force bypasses the gate
        market_gate.require_trading_day(date(2026, 9, 7), force=True, data_root=root)
        print("require_trading_day(holiday, force=True) -> passed")

        # early-close math
        close = market_gate.market_close_et(date(2026, 11, 27), data_root=root)
        end = market_gate.option_capture_end_et(date(2026, 11, 27), data_root=root)
        assert (close.hour, close.minute) == (13, 0)
        assert (end.hour, end.minute) == (13, 20)
        print(f"early close 13:00 ET, option capture end {end:%H:%M} ET -> ok")

        # logging path: {LOG_ROOT}/{job}/dt={date}/{epoch}.log + stdout
        log_root = root / "logs"
        with get_run_logger("verify_core", date(2026, 9, 8), log_root=log_root) as log:
            log.log("job_start", job="verify_core")
            log.log("job_end", rows=3, bytes=1234, duration_s=0.001)
        files = list((log_root / "verify_core").glob("dt=2026-09-08/*.log"))
        assert len(files) == 1, files
        lines = files[0].read_text().strip().splitlines()
        events = [json.loads(line)["event"] for line in lines]
        assert events == ["job_start", "job_end"], events
        print(f"run log written to {files[0]} with events {events} -> ok")

    print("verify_core: ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
