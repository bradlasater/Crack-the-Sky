"""IBKR Flex executions: the two-step exchange, and joining fills to the tape.

Fully offline. The live probe that shaped this: SendRequest with a valid token
and a bogus query id returns 1014, while a bad token returns 1012/1015 -- the
codes are the only way to tell the two misconfigurations apart, so they are
surfaced with distinct hints rather than a generic failure.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger
from ingest.jobs import ibkr_executions as job

FIXTURE = Path(__file__).parent / "fixtures" / "flex_trades.xml"


def _settings(tmp_path: Path, **kw) -> Settings:
    return dataclasses.replace(
        Settings(massive_api_key="k", data_root=tmp_path, log_root=tmp_path / "logs",
                 ibkr_flex_token="TOKEN", ibkr_flex_query_id="123456",
                 ibkr_account_id="U27766163"),
        **kw,
    )


def _logger() -> JsonlLogger:
    return JsonlLogger(path=None, echo=False)


def _fail(code: str, msg: str = "nope") -> str:
    return (f"<FlexStatementResponse><Status>Fail</Status><ErrorCode>{code}"
            f"</ErrorCode><ErrorMessage>{msg}</ErrorMessage></FlexStatementResponse>")


def _sent(ref: str = "REF123") -> str:
    return (f"<FlexStatementResponse><Status>Success</Status>"
            f"<ReferenceCode>{ref}</ReferenceCode>"
            f"<Url>https://example.invalid/GetStatement</Url></FlexStatementResponse>")


# ---------------------------------------------------------------------------
# OPRA reconstruction -- what makes a fill joinable to option_trades
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"assetCategory": "OPT", "underlyingSymbol": "SPXW", "putCall": "P",
          "strike": "7600", "expiry": "20260918"}, "O:SPXW260918P07600000"),
        # IBKR also reports expiry as 16SEP26
        ({"assetCategory": "OPT", "underlyingSymbol": "SPY", "putCall": "C",
          "strike": "770", "expiry": "18SEP26"}, "O:SPY260918C00770000"),
        # fractional strikes exist on VIX
        ({"assetCategory": "OPT", "underlyingSymbol": "VIX", "putCall": "C",
          "strike": "16.5", "expiry": "20260916"}, "O:VIX260916C00016500"),
    ],
)
def test_opra_ticker_is_reconstructed(row: dict, expected: str) -> None:
    assert job.opra_ticker(row) == expected


@pytest.mark.parametrize(
    "row",
    [
        {"assetCategory": "STK", "underlyingSymbol": "AAPL"},
        {"assetCategory": "OPT", "underlyingSymbol": "SPY", "putCall": "C",
         "strike": "770", "expiry": "garbage"},
        {"assetCategory": "OPT", "underlyingSymbol": "", "putCall": "C",
         "strike": "770", "expiry": "20260918"},
    ],
)
def test_opra_ticker_returns_none_when_not_reconstructable(row: dict) -> None:
    assert job.opra_ticker(row) is None


def test_reconstructed_tickers_pass_the_repo_root_filter() -> None:
    """A fill is only joinable if it survives the same filter the tape uses."""
    from ingest.jobs import keep_ticker

    for row, _ in [
        ({"assetCategory": "OPT", "underlyingSymbol": "SPXW", "putCall": "P",
          "strike": "7600", "expiry": "20260918"}, None),
        ({"assetCategory": "OPT", "underlyingSymbol": "VIX", "putCall": "C",
          "strike": "16.5", "expiry": "20260916"}, None),
    ]:
        assert keep_ticker(job.opra_ticker(row))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parses_trades_and_filters_by_account() -> None:
    xml_text = FIXTURE.read_text()
    rows = job.parse_trades(xml_text, "U27766163")
    assert len(rows) == 2, "the U99999999 row belongs to another account"
    assert {r["trade_id"] for r in rows} == {"7712345", "7712346"}


def test_parses_all_accounts_when_unfiltered() -> None:
    assert len(job.parse_trades(FIXTURE.read_text())) == 3


def test_fields_are_mapped_and_typed() -> None:
    rows = {r["trade_id"]: r for r in job.parse_trades(FIXTURE.read_text(), "U27766163")}
    buy = rows["7712345"]
    assert buy["opra_ticker"] == "O:SPXW260918P07600000"
    assert buy["quantity"] == 2.0
    assert buy["trade_price"] == 41.5
    assert buy["commission"] == -1.84
    assert buy["open_close"] == "O"
    sell = rows["7712346"]
    assert sell["quantity"] == -5.0          # signed as IBKR reports it
    assert sell["realized_pnl"] == 112.5
    assert sell["opra_ticker"] == "O:SPY260918C00770000"


def test_missing_numbers_become_null_not_zero() -> None:
    """A blank commission must not silently read as free."""
    rows = job.parse_trades(FIXTURE.read_text())
    stk = next(r for r in rows if r["asset_class"] == "STK")
    assert stk["realized_pnl"] is None
    assert stk["strike"] is None


# ---------------------------------------------------------------------------
# The two-step exchange
# ---------------------------------------------------------------------------

def test_polls_while_the_statement_is_generating(monkeypatch, tmp_path) -> None:
    """1019 means 'not ready yet' and must be waited out, not raised."""
    calls = {"n": 0}

    def fake_get(url, params):
        if url.endswith("/SendRequest"):
            return _sent()
        calls["n"] += 1
        return _fail("1019", "in progress") if calls["n"] < 3 else FIXTURE.read_text()

    monkeypatch.setattr(job, "_get", fake_get)
    monkeypatch.setattr(job.time, "sleep", lambda s: None)
    xml_text = job.fetch_statement(_settings(tmp_path), _logger())
    assert "FlexQueryResponse" in xml_text
    assert calls["n"] == 3


@pytest.mark.parametrize(
    ("code", "hint"),
    [("1012", "regenerate"), ("1014", "IBKR_FLEX_QUERY_ID"),
     ("1015", "expired"), ("1020", "numeric")],
)
def test_fatal_codes_explain_the_specific_fix(code, hint, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(job, "_get", lambda url, params: _fail(code))
    with pytest.raises(job.FlexError, match=hint):
        job.fetch_statement(_settings(tmp_path), _logger())


def test_missing_token_and_query_id_are_named_separately(tmp_path) -> None:
    with pytest.raises(job.FlexError, match="IBKR_FLEX_TOKEN"):
        job.fetch_statement(_settings(tmp_path, ibkr_flex_token=None), _logger())
    with pytest.raises(job.FlexError, match="IBKR_FLEX_QUERY_ID"):
        job.fetch_statement(_settings(tmp_path, ibkr_flex_query_id=None), _logger())


def test_gives_up_rather_than_polling_forever(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(job, "_get",
                        lambda url, params: _sent() if url.endswith("/SendRequest")
                        else _fail("1019"))
    monkeypatch.setattr(job.time, "sleep", lambda s: None)
    with pytest.raises(job.FlexError, match="still generating"):
        job.fetch_statement(_settings(tmp_path), _logger())


def test_empty_statement_is_not_a_failure() -> None:
    """A day with no fills is normal and must not page anyone."""
    empty = ('<FlexQueryResponse><FlexStatements count="1"><FlexStatement '
             'accountId="U27766163"><Trades/></FlexStatement></FlexStatements>'
             '</FlexQueryResponse>')
    assert job.parse_trades(empty, "U27766163") == []


# ---------------------------------------------------------------------------
# The token must never reach a log, an exception, or Healthchecks
# ---------------------------------------------------------------------------

def test_http_errors_never_carry_the_token(monkeypatch, tmp_path) -> None:
    """requests puts the prepared URL (with t=<token>) into HTTPError.

    run_job logs str(exc) *and* posts it as the Healthchecks failure body,
    which leaves the box -- so the token must be stripped at this boundary.
    """
    import requests

    token = "573620193207778458194644"

    class _Resp:
        status_code = 500

        def raise_for_status(self):
            raise requests.HTTPError(
                f"500 Server Error for url: https://x/GetStatement?t={token}&q=R&v=3",
                response=self,
            )

    monkeypatch.setattr(job.requests, "get", lambda *a, **k: _Resp())
    with pytest.raises(job.FlexError) as exc:
        job.fetch_statement(_settings(tmp_path, ibkr_flex_token=token), _logger())
    assert token not in str(exc.value)
    assert "GetStatement" in str(exc.value) or "SendRequest" in str(exc.value)
    assert "?" not in str(exc.value), "query string must be dropped entirely"


def test_network_errors_never_carry_the_token(monkeypatch, tmp_path) -> None:
    import requests

    token = "573620193207778458194644"

    def boom(*a, **k):
        raise requests.ConnectionError(f"failed connecting to https://x?t={token}")

    monkeypatch.setattr(job.requests, "get", boom)
    with pytest.raises(job.FlexError) as exc:
        job.fetch_statement(_settings(tmp_path, ibkr_flex_token=token), _logger())
    assert token not in str(exc.value)


# ---------------------------------------------------------------------------
# Account isolation
# ---------------------------------------------------------------------------

def test_row_without_account_id_is_excluded_when_filtering() -> None:
    """Absent accountId must not bypass the filter it is supposed to satisfy."""
    xml_text = (
        '<FlexQueryResponse><FlexStatements><FlexStatement accountId="U27766163">'
        '<Trades>'
        '<Trade tradeID="1" symbol="X" assetCategory="STK" quantity="1"/>'
        '<Trade accountId="U27766163" tradeID="2" symbol="Y" assetCategory="STK" quantity="1"/>'
        '</Trades></FlexStatement></FlexStatements></FlexQueryResponse>'
    )
    kept = job.parse_trades(xml_text, "U27766163")
    assert [r["trade_id"] for r in kept] == ["2"]
    # Unfiltered still sees both.
    assert len(job.parse_trades(xml_text)) == 2


# ---------------------------------------------------------------------------
# --date cannot relabel a statement
# ---------------------------------------------------------------------------

def test_statement_period_is_read_from_the_xml() -> None:
    assert job.statement_period(FIXTURE.read_text()) == ("2026-08-28", "2026-08-28")


def test_mismatched_date_is_rejected(monkeypatch, tmp_path) -> None:
    """The saved Flex period decides the content; --date must not relabel it."""
    import argparse

    monkeypatch.setattr(job, "fetch_statement", lambda s, log: FIXTURE.read_text())
    args = argparse.Namespace(date="2026-08-01", limit=None, dry_run=True,
                              force=True, underlying=None)
    with pytest.raises(ValueError, match="does not match the statement period"):
        job._main_fn(args, _settings(tmp_path), _logger())


def test_partition_follows_the_statement_not_the_clock(monkeypatch, tmp_path) -> None:
    import argparse

    monkeypatch.setattr(job, "fetch_statement", lambda s, log: FIXTURE.read_text())
    args = argparse.Namespace(date=None, limit=None, dry_run=True,
                              force=True, underlying=None)
    out = job._main_fn(args, _settings(tmp_path), _logger())
    assert out["date"] == "2026-08-28"


# ---------------------------------------------------------------------------
# The write path -- untested before, and it could not succeed
# ---------------------------------------------------------------------------

def test_non_dry_run_lands_raw_xml_verbatim_and_clean_parquet(monkeypatch, tmp_path) -> None:
    """Regression: write_raw() has no fmt= and JSON-encodes iterables.

    Passing the XML string to it raised TypeError, and without fmt it would
    have written one JSON line per character instead of the document.
    """
    import argparse

    import pyarrow.parquet as pq

    xml_text = FIXTURE.read_text()
    monkeypatch.setattr(job, "fetch_statement", lambda s, log: xml_text)
    args = argparse.Namespace(date=None, limit=None, dry_run=False,
                              force=True, underlying=None)
    out = job._main_fn(args, _settings(tmp_path), _logger())
    assert out["rows"] == 2
    assert out["opra_resolved"] == 2

    raw = list((tmp_path / "raw" / "ibkr_executions" / "dt=2026-08-28").glob("*.xml"))
    assert len(raw) == 1, "raw XML must land with an .xml extension"
    assert raw[0].read_text() == xml_text, "raw payload must be byte-for-byte"

    clean = list((tmp_path / "clean" / "ibkr_executions" / "dt=2026-08-28").glob("*.parquet"))
    assert len(clean) == 1
    rows = {r["trade_id"]: r for r in pq.read_table(clean[0]).to_pylist()}
    assert rows["7712345"]["opra_ticker"] == "O:SPXW260918P07600000"
    assert rows["7712346"]["realized_pnl"] == 112.5


def test_a_day_with_no_fills_writes_nothing_and_succeeds(monkeypatch, tmp_path) -> None:
    import argparse

    empty = ('<FlexQueryResponse><FlexStatements><FlexStatement accountId="U27766163" '
             'fromDate="20260828" toDate="20260828"><Trades/></FlexStatement>'
             '</FlexStatements></FlexQueryResponse>')
    monkeypatch.setattr(job, "fetch_statement", lambda s, log: empty)
    args = argparse.Namespace(date=None, limit=None, dry_run=False,
                              force=True, underlying=None)
    assert job._main_fn(args, _settings(tmp_path), _logger())["rows"] == 0
    assert not (tmp_path / "clean" / "ibkr_executions").exists()
