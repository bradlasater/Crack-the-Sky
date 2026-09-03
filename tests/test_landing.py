"""Landing-zone writer tests: the raw zone is append-only, never rewritten.

The clean writers already nudge their epoch-ms stamp on a same-millisecond
collision (see ``landing._unique_clean_path``); these pin the equivalent
guarantees for the raw writers.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ingest.common import landing

DT = date(2026, 8, 28)


@pytest.fixture()
def frozen_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the case deterministically rather than leaving it to timing."""
    monkeypatch.setattr(landing, "_epoch_ms", lambda: 1788298047097)


def test_write_raw_appends_on_a_same_millisecond_rerun(
    tmp_path: Path, frozen_ms: None
) -> None:
    """Records land cumulatively: a rerun appends, it does not replace."""
    landing.write_raw("holidays", DT, [{"a": 1}], job="holidays_sync",
                      data_root=tmp_path)
    path = landing.write_raw("holidays", DT, [{"a": 2}], job="holidays_sync",
                             data_root=tmp_path)
    assert path.read_text(encoding="utf-8").splitlines() == ['{"a": 1}', '{"a": 2}']


def test_write_raw_text_never_overwrites(tmp_path: Path, frozen_ms: None) -> None:
    """A whole-document payload cannot be appended to, so the stamp nudges.

    Two same-millisecond writes must both survive -- the raw zone is the
    record of truth and an overwrite would silently lose the first payload.
    """
    first = landing.write_raw_text("flex_executions", DT, "<xml>first</xml>",
                                   job="ibkr_executions", ext="xml",
                                   data_root=tmp_path)
    second = landing.write_raw_text("flex_executions", DT, "<xml>second</xml>",
                                    job="ibkr_executions", ext="xml",
                                    data_root=tmp_path)
    assert first != second
    assert first.read_text(encoding="utf-8") == "<xml>first</xml>"
    assert second.read_text(encoding="utf-8") == "<xml>second</xml>"
