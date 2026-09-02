"""Shared pytest plumbing: repo-root import path and frozen-fixture loading.

All tests run fully offline, and the ``_offline`` autouse fixture below now
*enforces* that rather than trusting it: production credentials are scrubbed
from both the environment and any parsed .env, and any outbound socket
connection raises. Fixtures are frozen JSON captures of real API responses
(arrays truncated to <= 3 items).
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ingest.common import config, ratelimit  # noqa: E402 - needs REPO_ROOT on sys.path

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Load a frozen JSON fixture payload by file name."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Offline enforcement
# ---------------------------------------------------------------------------
#
# The module docstring above has always claimed these tests run fully offline.
# It was not true: jobs resolve configuration from ``./.env`` (Settings.load
# defaults to it, and pricing.drift_check parses it directly), so a test that
# exercised a real ``main()`` from the repo root picked up the production
# HEALTHCHECKS_PING_KEY and posted genuine start/success/fail pings to
# hc-ping.com. tests/pricing/test_drift_check.py alone sent six per run, and
# the trailing /fail left the production "massive drift_check" check reading
# DOWN when nothing was wrong with it.
#
# Two layers below, because either alone would leave a hole: scrub the
# credentials so nothing can be addressed, and block the socket so nothing
# can leave the box even if some future code path finds a key another way.

# Secrets that must never reach a test. Scrubbed from both the process
# environment and any .env the code parses on its own.
_PRODUCTION_SECRETS = (
    "HEALTHCHECKS_PING_KEY",
    "HEALTHCHECKS_API_KEY",
    "HEALTHCHECKS_BASE",
    "MASSIVE_API_KEY",
    "IBKR_FLEX_TOKEN",
    "IBKR_ACCOUNT_ID",
    "IBKR_FLEX_QUERY_ID",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)


class OfflineTestViolation(AssertionError):
    """A test tried to open a connection to something outside the box."""


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path) -> None:
    """Scrub production credentials and refuse outbound network connections."""
    for name in _PRODUCTION_SECRETS:
        monkeypatch.delenv(name, raising=False)

    # The shared rate-limit bucket is file-backed and defaults under DATA_ROOT;
    # keep tests out of the box's real state.
    monkeypatch.setenv("MASSIVE_RATELIMIT_STATE", str(tmp_path / "ratelimit.json"))
    ratelimit.reset_default_bucket()

    real_parse = config._parse_env_file

    def _scrubbed(path: Path) -> dict[str, str]:
        values = real_parse(path)
        for name in _PRODUCTION_SECRETS:
            values.pop(name, None)
        return values

    monkeypatch.setattr(config, "_parse_env_file", _scrubbed)

    real_connect = socket.socket.connect

    def _blocked(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("localhost", "127.0.0.1", "::1"):
            raise OfflineTestViolation(
                f"outbound connection to {host!r} attempted in a test; "
                "monkeypatch the client instead of reaching the network"
            )
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _blocked)
