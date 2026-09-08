"""decision_log: append-only warehouse record. Never replace, never as-of."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ingest.common import landing
from ingest.common.decision_log import DATASET, write_decision, write_decisions
from ingest.schemas import decision_record
from marketdata.catalog import CatalogError, read_asof, read_partition

DT = date(2026, 9, 2)
ASOF_NS = 1_757_000_000_000_000_000


def _fields(**overrides: object) -> dict:
    rec = {
        "decision_id": "bt-1",
        "session_date": DT,
        "asof_ns": ASOF_NS,
        "src": "backtest",
        "job": "backtester",
        "gate": "no-trade",
        "rationale": "spread too wide",
        "inputs": {"spot": 765.16},
        "signals": {"atm_iv": 0.16},
        "versions": {"code": "pin"},
    }
    rec.update(overrides)
    return rec


def test_write_appends_a_second_file(tmp_path: Path) -> None:
    first = write_decision(data_root=tmp_path, **_fields(decision_id="a"))
    second = write_decision(data_root=tmp_path, **_fields(decision_id="b"))
    assert first != second
    assert first.is_file() and second.is_file()
    table = read_partition(DATASET, DT, data_root=tmp_path)
    assert table.num_rows == 2
    assert set(table["decision_id"].to_pylist()) == {"a", "b"}


def test_quarantine_prior_refuses_decision_log(tmp_path: Path) -> None:
    write_decision(data_root=tmp_path, **_fields())
    with pytest.raises(ValueError, match="append-only"):
        landing.quarantine_prior(DATASET, DT, "backtester", tmp_path)
    part = tmp_path / "clean" / DATASET / f"dt={DT.isoformat()}"
    assert list(part.glob("*.parquet")), "history must still be on disk"


def test_read_asof_refuses_decision_log(tmp_path: Path) -> None:
    write_decision(data_root=tmp_path, **_fields())
    with pytest.raises(CatalogError, match="append-only"):
        read_asof(DATASET, DT, data_root=tmp_path)


def test_missing_required_field_fails_loud() -> None:
    with pytest.raises(ValueError, match="decision_id"):
        decision_record(**_fields(decision_id=""))


def test_unknown_gate_fails_loud() -> None:
    with pytest.raises(ValueError, match="gate"):
        decision_record(**_fields(gate="hold"))


def test_unknown_src_fails_loud() -> None:
    with pytest.raises(ValueError, match="src"):
        decision_record(**_fields(src="sim"))


def test_json_fields_must_be_objects() -> None:
    with pytest.raises(ValueError, match="inputs"):
        decision_record(**_fields(inputs="{\"spot\": 1}"))
    with pytest.raises(ValueError, match="signals"):
        decision_record(**_fields(signals=[{"vrp": 0.01}]))


def test_unserializable_json_field_fails_loud() -> None:
    """A non-JSON value must name its field, not surface a bare TypeError."""
    with pytest.raises(ValueError, match="decision_log.signals is not JSON-serializable"):
        decision_record(**_fields(signals={"vrp": object()}))


def test_empty_batch_fails_loud() -> None:
    with pytest.raises(ValueError, match="empty batch"):
        write_decisions([], job="backtester")


def test_mixed_session_fails_loud(tmp_path: Path) -> None:
    a = decision_record(**_fields(decision_id="a"))
    b = decision_record(**_fields(decision_id="b", session_date="2026-09-03"))
    with pytest.raises(ValueError, match="session_date"):
        write_decisions([a, b], job="backtester", data_root=tmp_path)


def test_record_json_roundtrip() -> None:
    rec = decision_record(**_fields())
    assert json.loads(rec["inputs"]) == {"spot": 765.16}
    assert rec["session_date"] == "2026-09-02"
    assert rec["underlying"] is None
