"""JSONL run logging: one structured event per line, to file and stdout.

Run logs live at ``{LOG_ROOT}/{job}/dt={date}/{epoch}.log`` so cron output
stays greppable and machine-parseable.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


def _default_log_root() -> Path:
    return Path(os.environ.get("LOG_ROOT", os.environ.get("DATA_ROOT", "/data/massive") + "/logs"))


class JsonlLogger:
    """Append-only structured logger writing JSON lines to a file and stdout."""

    def __init__(self, path: Path | None = None, echo: bool = True) -> None:
        self.path = path
        self.echo = echo
        self._fh = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Long-lived append handle: the logger outlives any single
            # write and is closed by JsonlLogger.close().
            self._fh = open(path, "a", encoding="utf-8")  # noqa: SIM115

    def log(self, event: str, **fields: Any) -> dict[str, Any]:
        """Emit one event line: ``{"ts": ..., "event": ..., **fields}``."""
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": event,
            **fields,
        }
        line = json.dumps(record, default=str)
        if self._fh is not None:
            self._fh.write(line + "\n")
            self._fh.flush()
        if self.echo:
            print(line, file=sys.stdout, flush=True)
        return record

    def close(self) -> None:
        """Flush and close the underlying log file, if any."""
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> JsonlLogger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def get_run_logger(
    job: str,
    dt: date | str,
    log_root: str | os.PathLike[str] | None = None,
    echo: bool = True,
) -> JsonlLogger:
    """Create a run logger at ``{LOG_ROOT}/{job}/dt={date}/{epoch}.log``."""
    day = dt.isoformat() if isinstance(dt, date) else str(dt)
    root = Path(log_root) if log_root is not None else _default_log_root()
    path = root / job / f"dt={day}" / f"{int(time.time())}.log"
    return JsonlLogger(path, echo=echo)
