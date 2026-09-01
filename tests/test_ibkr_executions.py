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
