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


def test_write_raw_text_retries_on_a_preclaimed_path(
    tmp_path: Path, frozen_ms: None
) -> None:
    """A path claimed between stamp selection and the open must nudge, not truncate.

    The candidate is claimed with exclusive create ('xb'), so a FileExistsError
    on the first candidate -- here from a file already on disk, standing in for
    a concurrent writer -- retries with the next stamp and leaves the
    pre-existing payload intact.
    """
    out_dir = tmp_path / "raw" / "flex_executions" / f"dt={DT.isoformat()}"
    out_dir.mkdir(parents=True)
    preclaimed = out_dir / "ibkr_executions-1788298047097.xml"
    preclaimed.write_text("<xml>pre-existing</xml>", encoding="utf-8")

    path = landing.write_raw_text("flex_executions", DT, "<xml>new</xml>",
                                  job="ibkr_executions", ext="xml",
                                  data_root=tmp_path)

    assert path == out_dir / "ibkr_executions-1788298047098.xml"
    assert path.read_text(encoding="utf-8") == "<xml>new</xml>"
    assert preclaimed.read_text(encoding="utf-8") == "<xml>pre-existing</xml>"


# ---------------------------------------------------------------------------
# quarantine_prior
# ---------------------------------------------------------------------------

def _land_clean_bytes(root: Path, name: str, payload: bytes) -> Path:
    part = root / "clean" / "option_trades" / f"dt={DT.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    path = part / name
    path.write_bytes(payload)
    return path


def test_quarantine_prior_never_overwrites_a_same_named_file(tmp_path: Path) -> None:
    """Quarantining one partition twice must keep both versions recoverable.

    Path.replace overwrites a same-named target, so the second refilter used
    to silently destroy the first quarantined file -- the data the move
    exists to preserve. The stamp token nudges forward instead, keeping the
    name's shape for the readers that parse it as an integer.
    """
    name = "flatfile_pull-1788298047097.parquet"
    _land_clean_bytes(tmp_path, name, b"first")
    landing.quarantine_prior("option_trades", DT, "flatfile_pull", data_root=tmp_path)
    _land_clean_bytes(tmp_path, name, b"second")
    moved = landing.quarantine_prior("option_trades", DT, "flatfile_pull",
                                     data_root=tmp_path)

    dest = tmp_path / "_quarantine" / "refilter" / "option_trades" / f"dt={DT.isoformat()}"
    assert (dest / name).read_bytes() == b"first"
    assert moved == [dest / "flatfile_pull-1788298047098.parquet"]
    assert moved[0].read_bytes() == b"second"


def test_quarantine_prior_nudges_past_a_run_of_collisions(tmp_path: Path) -> None:
    name = "flatfile_pull-1788298047097.parquet"
    for i in range(3):
        _land_clean_bytes(tmp_path, name, f"v{i}".encode())
        landing.quarantine_prior("option_trades", DT, "flatfile_pull",
                                 data_root=tmp_path)
    dest = tmp_path / "_quarantine" / "refilter" / "option_trades" / f"dt={DT.isoformat()}"
    assert sorted(p.read_bytes() for p in dest.glob("*.parquet")) == [b"v0", b"v1", b"v2"]


def test_quarantine_prior_keeps_the_source_when_the_link_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """The source is unlinked only after its hard link lands.

    Quarantine claims each candidate with os.link so a concurrent move can
    never overwrite a placed file; if the link itself errors (the target fs
    is full, say), the source must stay where a retry can find it rather than
    be half-moved.
    """
    name = "flatfile_pull-1788298047097.parquet"
    src = _land_clean_bytes(tmp_path, name, b"first")

    def fail_link(s, d):
        raise OSError("no space left")

    monkeypatch.setattr(landing.os, "link", fail_link)
    with pytest.raises(OSError):
        landing.quarantine_prior("option_trades", DT, "flatfile_pull",
                                 data_root=tmp_path)
    assert src.is_file() and src.read_bytes() == b"first"


def test_quarantine_prior_retries_after_a_lost_link_race(
    tmp_path: Path, monkeypatch
) -> None:
    """A candidate claimed between selection and link must not lose the file.

    Simulates the TOCTOU race: another process hard-links its own file onto
    the first candidate just as this one tries. The move must fall through to
    the nudged stamp with both versions intact.
    """
    import os

    real_link = os.link
    raced = {"done": False}

    def racing_link(src, dst):
        if not raced["done"]:
            raced["done"] = True
            (tmp_path / "winner").write_bytes(b"winner")
            real_link(tmp_path / "winner", dst)
        return real_link(src, dst)

    name = "flatfile_pull-1788298047097.parquet"
    _land_clean_bytes(tmp_path, name, b"ours")
    monkeypatch.setattr(landing.os, "link", racing_link)
    moved = landing.quarantine_prior("option_trades", DT, "flatfile_pull",
                                     data_root=tmp_path)

    dest = tmp_path / "_quarantine" / "refilter" / "option_trades" / f"dt={DT.isoformat()}"
    assert (dest / name).read_bytes() == b"winner"
    assert moved == [dest / "flatfile_pull-1788298047098.parquet"]
    assert moved[0].read_bytes() == b"ours"
