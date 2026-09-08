"""decision_log: append-only decision record — builder validation, writer, read path.

The record exists before its first real writers (the week-2 backtester, the
strategy engine) so they land on a finished contract. The guarantees pinned
here are the ones the warehouse row in PLAN.md demands: history is never
overwritten (no quarantine, no as-of), and a batch is one session and one
job so a mixed write cannot land events in the wrong partition.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow", reason="pyarrow required for parquet writes")

from ingest import schemas  # noqa: E402
from ingest.common import decision_log, landing  # noqa: E402
from marketdata import catalog  # noqa: E402

DT = date(2026, 9, 4)
JOB = "backtest_v0"


def _record(**over: object) -> dict:
    fields = {
        "decision_id": "bt-0001",
        "session_date": DT,
        "asof_ns": 1_788_000_000_000_000_000,
        "src": "backtest",
        "job": JOB,
        "gate": "entry",
        "rationale": "vrp spread above threshold",
        "inputs": {"dte": 21, "spot": 765.16},
        "signals": {"vrp": 0.012},
        "versions": {"surface": "abc123"},
    }
    fields.update(over)
    return schemas.decision_record(**fields)


# ---------------------------------------------------------------------------
# decision_record: fail-loud builder
# ---------------------------------------------------------------------------

def test_decision_log_is_append_only() -> None:
    assert decision_log.DATASET in schemas.APPEND_ONLY_DATASETS


def test_session_date_accepts_date_or_iso_string() -> None:
    assert _record()["session_date"] == "2026-09-04"
    assert _record(session_date="2026-09-04")["session_date"] == "2026-09-04"


def test_session_date_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _record(session_date="09/04/2026")
    with pytest.raises(ValueError, match="date or YYYY-MM-DD"):
        _record(session_date=1788000000)


def test_src_and_gate_are_closed_vocabularies() -> None:
    with pytest.raises(ValueError, match="not one of"):
        _record(src="replay")
    with pytest.raises(ValueError, match="not one of"):
        _record(gate="hold")


def test_required_text_fields_refuse_blank() -> None:
    for field, value in (
        ("decision_id", ""),
        ("job", "  "),
        ("rationale", ""),
        ("code_version", ""),
    ):
        with pytest.raises(ValueError, match=field):
            _record(**{field: value})


def test_asof_ns_must_be_an_int() -> None:
    with pytest.raises(ValueError, match="asof_ns"):
        _record(asof_ns="1788000000000000000")
    with pytest.raises(ValueError, match="asof_ns"):
        _record(asof_ns=True)  # bool is an int subclass; not a timestamp


def test_nested_payloads_must_be_dicts() -> None:
    with pytest.raises(ValueError, match="inputs"):
        _record(inputs=[("dte", 21)])
    with pytest.raises(ValueError, match="signals"):
        _record(signals="vrp=0.012")


def test_versions_defaults_to_an_empty_object() -> None:
    fields = {
        "decision_id": "bt-0001",
        "session_date": DT,
        "asof_ns": 1,
        "src": "paper",
        "job": JOB,
        "gate": "no-trade",
        "rationale": "no edge net of costs",
        "inputs": {},
        "signals": {},
    }
    rec = schemas.decision_record(**fields)
    assert json.loads(rec["versions"]) == {}


# ---------------------------------------------------------------------------
# write_decisions: one session, one job, never quarantined
# ---------------------------------------------------------------------------

def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    path = decision_log.write_decisions([_record()], job=JOB, data_root=tmp_path)
    assert path.parent == tmp_path / "clean" / "decision_log" / f"dt={DT.isoformat()}"
    table = decision_log.read_decisions(DT, data_root=tmp_path)
    assert table.num_rows == 1
    assert table["decision_id"].to_pylist() == ["bt-0001"]
    assert json.loads(table["inputs"].to_pylist()[0]) == {"dte": 21, "spot": 765.16}


def test_a_second_write_is_a_second_file_not_an_overwrite(tmp_path: Path) -> None:
    """Do not overwrite decision history: appends accumulate as new files."""
    first = decision_log.write_decisions([_record()], job=JOB, data_root=tmp_path)
    second = decision_log.write_decisions(
        [_record(decision_id="bt-0002", gate="exit", rationale="target hit")],
        job=JOB,
        data_root=tmp_path,
    )
    assert first != second
    assert first.is_file() and second.is_file()
    table = decision_log.read_decisions(DT, data_root=tmp_path)
    assert sorted(table["decision_id"].to_pylist()) == ["bt-0001", "bt-0002"]


def test_write_decision_single_row_helper(tmp_path: Path) -> None:
    decision_log.write_decision(
        data_root=tmp_path,
        decision_id="bt-0001",
        session_date=DT,
        asof_ns=1_788_000_000_000_000_000,
        src="backtest",
        job=JOB,
        gate="entry",
        rationale="vrp spread above threshold",
        inputs={"dte": 21},
        signals={"vrp": 0.012},
    )
    table = decision_log.read_decisions(DT, data_root=tmp_path)
    assert table.num_rows == 1
    assert table["job"].to_pylist() == [JOB]


def test_empty_batch_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty batch"):
        decision_log.write_decisions([], job=JOB, data_root=tmp_path)


def test_mixed_session_dates_are_refused(tmp_path: Path) -> None:
    rows = [_record(), _record(decision_id="bt-0002", session_date="2026-09-03")]
    with pytest.raises(ValueError, match="mixes session_date"):
        decision_log.write_decisions(rows, job=JOB, data_root=tmp_path)


def test_missing_session_date_is_refused(tmp_path: Path) -> None:
    row = _record()
    row["session_date"] = None
    with pytest.raises(ValueError, match="session_date"):
        decision_log.write_decisions([row], job=JOB, data_root=tmp_path)


def test_job_mismatch_is_refused(tmp_path: Path) -> None:
    """A row tagged with another job would land under this job's filename."""
    with pytest.raises(ValueError, match="does not match write job"):
        decision_log.write_decisions([_record()], job="strategy_v0", data_root=tmp_path)


# ---------------------------------------------------------------------------
# The append-only rule on the shared paths
# ---------------------------------------------------------------------------

def test_quarantine_prior_refuses_decision_log(tmp_path: Path) -> None:
    decision_log.write_decisions([_record()], job=JOB, data_root=tmp_path)
    with pytest.raises(ValueError, match="append-only"):
        landing.quarantine_prior("decision_log", DT, JOB, data_root=tmp_path)
    # The refusal must not have moved anything.
    table = decision_log.read_decisions(DT, data_root=tmp_path)
    assert table.num_rows == 1


def test_read_asof_refuses_decision_log(tmp_path: Path) -> None:
    """Last-file-wins would hide earlier events; read the whole partition."""
    decision_log.write_decisions([_record()], job=JOB, data_root=tmp_path)
    with pytest.raises(catalog.CatalogError, match="append-only"):
        catalog.read_asof("decision_log", DT, data_root=tmp_path)


def test_read_decisions_missing_partition_is_loud(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="no parquet"):
        decision_log.read_decisions(DT, data_root=tmp_path)


def test_unknown_dataset_fails_loud_on_write(tmp_path: Path) -> None:
    """write_clean projected a KeyError before; now the message names the dataset."""
    with pytest.raises(ValueError, match="unknown dataset 'not_a_dataset'"):
        landing.write_clean("not_a_dataset", DT, [{}], job=JOB, data_root=tmp_path)
