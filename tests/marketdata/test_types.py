"""Quote carries the trade's own stamp alongside the snapshot clock."""

from __future__ import annotations

from marketdata.types import Quote, quotes_from_snapshot_rows
from tests.marketdata.conftest import snapshot_row

UNDERLYING_STAMP_NS = 1_700_000_100_000_000_000
TRADE_STAMP_NS = 1_700_000_000_000_000_000


def test_trade_stamp_carried_onto_quote() -> None:
    rec = snapshot_row("O:SPY260831C00420000")
    rec["underlying_last_updated_ns"] = UNDERLYING_STAMP_NS
    rec["last_trade_sip_timestamp_ns"] = TRADE_STAMP_NS
    (q,) = quotes_from_snapshot_rows([rec])
    # asof_ns semantics unchanged: the snapshot clock prefers the underlying
    # stamp, and the trade's own stamp rides next to it.
    assert q.asof_ns == UNDERLYING_STAMP_NS
    assert q.last_trade_asof_ns == TRADE_STAMP_NS


def test_trade_stamp_defaults_to_none() -> None:
    (q,) = quotes_from_snapshot_rows([snapshot_row("O:SPY260831C00420000")])
    assert q.last_trade_asof_ns is None
    # Keyword construction without the new field stays source-compatible.
    q2 = Quote(
        contract=q.contract,
        last=q.last,
        day_close=q.day_close,
        underlying_price=q.underlying_price,
        asof_ns=q.asof_ns,
        open_interest=q.open_interest,
    )
    assert q2.last_trade_asof_ns is None
