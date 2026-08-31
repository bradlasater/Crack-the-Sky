"""Shared pytest plumbing: repo-root import path and frozen-fixture loading.

All tests run fully offline: HTTP is monkeypatched, fixtures are frozen JSON
captures of real API responses (arrays truncated to <= 3 items).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Load a frozen JSON fixture payload by file name."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
