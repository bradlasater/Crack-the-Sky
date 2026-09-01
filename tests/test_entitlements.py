"""Entitlement probe: bootstrap states SKIP, and probes do not rot.

Both regressions covered here make the probe report the wrong thing rather
than crash loudly, which is the worst failure mode for a tool whose only job
is to tell you what your plan can reach.
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from ingest.common.config import Settings
from ingest.entitlements import (
    PROBES,
    _resolve_probe_contract,
    _s3_credentials_missing,
)


def _settings(**kw) -> Settings:
    return dataclasses.replace(Settings(massive_api_key="k"), **kw)


# ---------------------------------------------------------------------------
# S3 probes must SKIP, not FAIL, before credentials exist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"massive_s3_access_key_id": None,
          "massive_s3_secret_access_key": None}, "no S3 credentials"),
        # Only the access key was checked before, so a half-configured .env
        # sent a request with no secret and got a verdict about entitlement.
        ({"massive_s3_access_key_id": "AK",
          "massive_s3_secret_access_key": None}, "no S3 credentials"),
        ({"massive_s3_access_key_id": None,
          "massive_s3_secret_access_key": "SK"}, "no S3 credentials"),
        # A freshly bootstrapped .env carries the dashboard placeholders.
        ({"massive_s3_access_key_id": "REPLACE_ME_FROM_MASSIVE_DASHBOARD",
          "massive_s3_secret_access_key": "REPLACE_ME_FROM_MASSIVE_DASHBOARD"},
         "placeholder S3 credentials"),
        ({"massive_s3_access_key_id": "AK",
          "massive_s3_secret_access_key": "REPLACE_ME_X"},
         "placeholder S3 credentials"),
    ],
)
def test_bootstrap_states_are_reported_as_skip(kw: dict, expected: str) -> None:
    assert _s3_credentials_missing(_settings(**kw)) == expected


def test_real_credentials_are_probed() -> None:
    assert _s3_credentials_missing(
        _settings(massive_s3_access_key_id="AK", massive_s3_secret_access_key="SK")
    ) is None


# ---------------------------------------------------------------------------
# Option probes must not be pinned to an expiring contract
# ---------------------------------------------------------------------------

def test_no_probe_hardcodes_an_option_ticker() -> None:
    """A fixed expiry becomes a permanent CI failure the day it expires.

    The aggregate request then returns an entitled-but-empty 200, which the
    body validator cannot tell apart from a revoked entitlement.
    """
    for name, template, _expected, _validate in PROBES:
        assert "O:SPY2" not in template, f"{name} pins a dated contract"
        assert "O:SPX2" not in template, f"{name} pins a dated contract"


def test_option_probes_use_the_resolved_contract() -> None:
    templated = [n for n, t, _e, _v in PROBES if "{contract}" in t]
    assert {"trades/option", "aggs/option minute T-1"} <= set(templated)


class _Resp:
    def __init__(self, payload, ok=True):
        self._payload, self.ok = payload, ok

    def json(self):
        return self._payload


def _chain(*rows):
    return {"results": [
        {"details": {"ticker": t, "expiration_date": e}, "day": {"volume": v}}
        for t, e, v in rows
    ]}


def test_resolver_picks_the_busiest_unexpired_contract(monkeypatch) -> None:
    import ingest.entitlements as mod

    monkeypatch.setattr(mod.requests, "get", lambda *a, **k: _Resp(_chain(
        ("O:SPY260901C00700000", "2026-09-01", 10),
        ("O:SPY260918C00770000", "2026-09-18", 500),
        ("O:SPY261016C00800000", "2026-10-16", 3),
    )))
    ticker, detail = _resolve_probe_contract(_settings(), date(2026, 8, 31))
    assert ticker == "O:SPY260918C00770000"
    assert "500" in detail


def test_resolver_ignores_contracts_expired_on_the_probe_date(monkeypatch) -> None:
    """An expired contract has no bars, however much it once traded."""
    import ingest.entitlements as mod

    monkeypatch.setattr(mod.requests, "get", lambda *a, **k: _Resp(_chain(
        ("O:SPY260801C00700000", "2026-08-01", 99999),   # long expired
        ("O:SPY260918C00770000", "2026-09-18", 12),
    )))
    ticker, _ = _resolve_probe_contract(_settings(), date(2026, 8, 31))
    assert ticker == "O:SPY260918C00770000"


def test_resolver_reports_why_it_could_not_resolve(monkeypatch) -> None:
    import ingest.entitlements as mod

    monkeypatch.setattr(mod.requests, "get", lambda *a, **k: _Resp(_chain(
        ("O:SPY260801C00700000", "2026-08-01", 5),
    )))
    ticker, detail = _resolve_probe_contract(_settings(), date(2026, 8, 31))
    assert ticker is None
    assert "no unexpired" in detail


def test_resolver_survives_a_network_failure(monkeypatch) -> None:
    import requests as rq

    import ingest.entitlements as mod

    def boom(*a, **k):
        raise rq.ConnectionError("down")

    monkeypatch.setattr(mod.requests, "get", boom)
    ticker, detail = _resolve_probe_contract(_settings(), date(2026, 8, 31))
    assert ticker is None
    assert "ConnectionError" in detail
