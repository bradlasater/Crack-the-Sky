"""Tests for contracts_sync's universe diff.

The diff is the only signal that the contract universe moved -- new listings
appearing, expiries dropping off. It was wrong for every underlying but the
first one synced, and wrong in the direction that hides loss: ``gone`` was
structurally always zero.
"""

from __future__ import annotations

from datetime import date

from ingest.common import landing
from ingest.common.config import Settings
from ingest.jobs import contracts_sync
from ingest.jobs.contracts_sync import _previous_tickers

YESTERDAY = date(2026, 9, 1)
TODAY = date(2026, 9, 2)


def _settings(data_root) -> Settings:
    return Settings(
        massive_api_key="test-key",
        data_root=data_root,
        log_root=data_root / "logs",
    )


def _contract(ticker: str, underlying: str) -> dict:
    return {
        "ticker": ticker,
        "underlying_ticker": underlying,
        "contract_type": "call",
        "exercise_style": "european",
        "expiration_date": "2026-09-18",
        "strike_price": 100.0,
        "shares_per_contract": 100,
        "primary_exchange": "OPRA",
        "cfi": "OCEXXX",
    }


def _land(data_root, day: date, underlying: str, tickers: list[str]) -> None:
    landing.write_clean(
        "contracts",
        day,
        [_contract(t, underlying) for t in tickers],
        job=f"contracts_sync-{underlying}",
        data_root=data_root,
    )


def test_baseline_survives_a_partial_same_day_partition(tmp_path) -> None:
    """The 08:00 bug: SPY writes today's partition, SPX then finds nothing.

    Underlyings share one dt= partition and are synced in order. Taking "the
    latest partition" meant the second and third passes diffed against a
    partition that held only the first pass's rows, so their whole universe
    reported as new with gone=0 -- SPX new=28642 on 2026-09-02, on a day
    nothing happened.
    """
    settings = _settings(tmp_path)
    _land(tmp_path, YESTERDAY, "SPY", ["O:SPY1", "O:SPY2"])
    _land(tmp_path, YESTERDAY, "SPX", ["O:SPX1", "O:SPX2", "O:SPX3"])

    # SPY's pass has already written into today's partition.
    _land(tmp_path, TODAY, "SPY", ["O:SPY1", "O:SPY2"])

    # SPX now diffs -- and must still see yesterday's SPX universe.
    assert _previous_tickers(settings, "contracts", TODAY, "SPX") == {
        "O:SPX1", "O:SPX2", "O:SPX3",
    }


def test_delistings_are_visible(tmp_path) -> None:
    """``gone`` must be able to be non-zero, or it monitors nothing."""
    settings = _settings(tmp_path)
    _land(tmp_path, YESTERDAY, "SPX", ["O:SPX1", "O:SPX2", "O:SPX3"])
    _land(tmp_path, TODAY, "SPY", ["O:SPY1"])

    previous = _previous_tickers(settings, "contracts", TODAY, "SPX")
    current = {"O:SPX1"}
    assert len(previous - current) == 2


def test_same_day_second_run_uses_the_first_run(tmp_path) -> None:
    """16:30 must diff against 08:00, not against yesterday."""
    settings = _settings(tmp_path)
    _land(tmp_path, YESTERDAY, "SPX", ["O:SPX1"])
    _land(tmp_path, TODAY, "SPX", ["O:SPX1", "O:SPX2"])

    assert _previous_tickers(settings, "contracts", TODAY, "SPX") == {
        "O:SPX1", "O:SPX2",
    }


def test_first_ever_run_has_an_empty_baseline(tmp_path) -> None:
    """A genuinely new underlying reports its whole universe as new, once."""
    settings = _settings(tmp_path)
    assert _previous_tickers(settings, "contracts", TODAY, "VIX") == set()


def test_baseline_ignores_partitions_after_the_run_date(tmp_path) -> None:
    settings = _settings(tmp_path)
    _land(tmp_path, YESTERDAY, "SPX", ["O:SPX1"])
    _land(tmp_path, date(2026, 9, 10), "SPX", ["O:SPX_FUTURE"])
    assert _previous_tickers(settings, "contracts", TODAY, "SPX") == {"O:SPX1"}


# ---------------------------------------------------------------------------
# The expired pass and the trading-day gate
# ---------------------------------------------------------------------------


def _argv_handed_to_run_job(monkeypatch, argv: list[str]) -> list[str]:
    """What ``main`` passes to ``run_job``, without running the job."""
    seen: list[str] = []

    def fake_run_job(job_name, main_fn, parsed):
        seen.extend(parsed)

    monkeypatch.setattr(contracts_sync, "run_job", fake_run_job)
    contracts_sync.main(argv)
    return seen


def test_the_expired_pass_forces_past_the_trading_day_gate(monkeypatch) -> None:
    """The regression: a Saturday-only job that the Saturday gate discarded.

    ``--expired`` is scheduled ``00 09 * * 6`` and nothing else. Saturday is
    never a trading day, so ``run_job``'s gate raised SystemExit(0) before any
    work -- and because that exit pings Healthchecks *ok* (so market holidays
    do not page), the check stayed green while ``clean/contracts_expired`` was
    never created at all.
    """
    assert "--force" in _argv_handed_to_run_job(monkeypatch, ["--expired"])


def test_the_weekday_passes_stay_gated(monkeypatch) -> None:
    """08:00 and 16:30 fire Mon-Fri, market calendar or not.

    Forcing those too would write a contracts partition on Thanksgiving, which
    is the exact thing the gate exists to prevent. Only the expired pass --
    reference data whose subject is the universe, not a session -- opts out.
    """
    assert "--force" not in _argv_handed_to_run_job(monkeypatch, [])


def test_an_explicit_force_is_not_duplicated(monkeypatch) -> None:
    assert _argv_handed_to_run_job(monkeypatch, ["--expired", "--force"]).count("--force") == 1
