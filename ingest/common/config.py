"""Runtime configuration loaded from .env and the process environment.

`Settings.load()` reads a .env file with a minimal ``KEY=VAL`` parser (no
third-party dependency) and then lets real environment variables override
file values. Missing required variables cause ``SystemExit(2)`` with a
message naming the offending variable.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_API_BASE = "https://api.polygon.io"
DEFAULT_S3_ENDPOINT = "https://files.massive.com"
DEFAULT_S3_BUCKET = "flatfiles"
DEFAULT_DATA_ROOT = "/data/massive"
DEFAULT_WS_DELAYED_URL = "wss://delayed.massive.com/options"
DEFAULT_HEALTHCHECKS_BASE = "https://hc-ping.com"
DEFAULT_IBKR_FLEX_BASE = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple dotenv-style file (KEY=VAL, '#' comments, optional quotes)."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            values[key] = val
    return values


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings for all ingestion jobs."""

    massive_api_key: str
    massive_api_base: str = DEFAULT_API_BASE
    massive_s3_access_key_id: str | None = None
    massive_s3_secret_access_key: str | None = None
    massive_s3_endpoint: str = DEFAULT_S3_ENDPOINT
    massive_s3_bucket: str = DEFAULT_S3_BUCKET
    data_root: Path = field(default=Path(DEFAULT_DATA_ROOT))
    log_root: Path | None = None  # default: {data_root}/logs
    healthchecks_ping_key: str | None = None
    healthchecks_base: str = DEFAULT_HEALTHCHECKS_BASE
    ibkr_flex_token: str | None = None
    ibkr_flex_query_id: str | None = None
    ibkr_account_id: str | None = None
    ibkr_flex_base: str = DEFAULT_IBKR_FLEX_BASE
    ws_delayed_url: str = DEFAULT_WS_DELAYED_URL

    def __post_init__(self) -> None:
        if self.log_root is None:
            object.__setattr__(self, "log_root", Path(self.data_root) / "logs")

    @classmethod
    def load(cls, env_path: str | os.PathLike[str] | None = None) -> Settings:
        """Build Settings from .env then environment overrides.

        Args:
            env_path: explicit .env path; defaults to ``./.env``. Values from
                the file are overridden by ``os.environ``.

        Raises:
            SystemExit: code 2 when a required variable (``MASSIVE_API_KEY``)
                is missing, or when legacy ``HEALTHCHECKS_PING_URL`` is set
                without ``HEALTHCHECKS_PING_KEY``.
        """
        path = Path(env_path) if env_path is not None else Path(".env")
        file_vals = _parse_env_file(path)

        def get(name: str, default: str | None = None) -> str | None:
            return os.environ.get(name, file_vals.get(name, default))

        api_key = get("MASSIVE_API_KEY")
        if not api_key:
            print(
                "ERROR: required environment variable MASSIVE_API_KEY is not set "
                f"(looked in {path} and the process environment).",
                file=sys.stderr,
            )
            sys.exit(2)

        data_root = Path(get("DATA_ROOT", DEFAULT_DATA_ROOT) or DEFAULT_DATA_ROOT)
        log_root_raw = get("LOG_ROOT")
        log_root = Path(log_root_raw) if log_root_raw else data_root / "logs"

        # Several helpers (landing._data_root, logging_utils._default_log_root)
        # resolve these from os.environ so their signatures work bare. Values
        # that came from the .env file are not in the process environment yet,
        # so export them here -- otherwise a non-default DATA_ROOT in .env
        # silently splits raw/clean data from the logs.
        os.environ.setdefault("DATA_ROOT", str(data_root))
        os.environ.setdefault("LOG_ROOT", str(log_root))

        ping_key = get("HEALTHCHECKS_PING_KEY") or None
        # Shared-URL mode hid dead jobs (one success greened the only check).
        # Dropping it silently would also hide missed runs, so leftover
        # HEALTHCHECKS_PING_URL without a ping key fails at load with a
        # migration path rather than disabling monitoring unnoticed.
        if get("HEALTHCHECKS_PING_URL") and not ping_key:
            print(
                "ERROR: HEALTHCHECKS_PING_URL is no longer supported. "
                "Set HEALTHCHECKS_PING_KEY instead (Healthchecks project -> "
                "Settings -> Ping Key) so each job gets its own check. A "
                "single shared URL hides jobs that stop running.",
                file=sys.stderr,
            )
            sys.exit(2)

        return cls(
            massive_api_key=api_key,
            massive_api_base=get("MASSIVE_API_BASE", DEFAULT_API_BASE) or DEFAULT_API_BASE,
            massive_s3_access_key_id=get("MASSIVE_S3_ACCESS_KEY_ID") or None,
            massive_s3_secret_access_key=get("MASSIVE_S3_SECRET_ACCESS_KEY") or None,
            massive_s3_endpoint=get("MASSIVE_S3_ENDPOINT", DEFAULT_S3_ENDPOINT) or DEFAULT_S3_ENDPOINT,
            massive_s3_bucket=get("MASSIVE_S3_BUCKET", DEFAULT_S3_BUCKET) or DEFAULT_S3_BUCKET,
            data_root=data_root,
            log_root=log_root,
            healthchecks_ping_key=ping_key,
            ibkr_flex_token=get("IBKR_FLEX_TOKEN") or None,
            ibkr_flex_query_id=get("IBKR_FLEX_QUERY_ID") or None,
            ibkr_account_id=get("IBKR_ACCOUNT_ID") or None,
            ibkr_flex_base=(get("IBKR_FLEX_BASE", DEFAULT_IBKR_FLEX_BASE)
                            or DEFAULT_IBKR_FLEX_BASE).rstrip("/"),
            healthchecks_base=(get("HEALTHCHECKS_BASE", DEFAULT_HEALTHCHECKS_BASE)
                               or DEFAULT_HEALTHCHECKS_BASE).rstrip("/"),
            ws_delayed_url=get("WS_DELAYED_URL", DEFAULT_WS_DELAYED_URL) or DEFAULT_WS_DELAYED_URL,
        )
