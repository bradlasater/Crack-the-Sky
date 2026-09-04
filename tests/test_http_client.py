"""MassiveClient tests: retries, 403 entitlement errors, apiKey re-append.

Fully offline: the requests session is replaced with a scripted fake and
``time.sleep`` is monkeypatched to a no-op.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from ingest.common import http_client
from ingest.common.config import Settings
from ingest.common.http_client import MassiveClient

API_KEY = "test-key-123"


def make_settings() -> Settings:
    return Settings(massive_api_key=API_KEY)


class FakeResponse:
    def __init__(self, status_code: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = "" if not body else str(body)

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Scripted session: pops one response/exception per get() call."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, timeout: int | None = None, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, "timeout": timeout, **kwargs})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_client.time, "sleep", lambda _s: None)


@pytest.fixture()
def client() -> MassiveClient:
    return MassiveClient(make_settings())


def test_get_appends_api_key(client: MassiveClient) -> None:
    client.session = FakeSession([FakeResponse(200, {"status": "OK", "results": []})])
    body = client.get("/v3/reference/options/contracts", {"underlying_ticker": "SPY"})
    assert body["status"] == "OK"
    url = client.session.calls[0]["url"]
    assert url.startswith("https://api.polygon.io/v3/reference/options/contracts?")
    assert f"apiKey={API_KEY}" in url
    assert "underlying_ticker=SPY" in url
    assert client.session.calls[0]["timeout"] == http_client.TIMEOUT_S


def test_get_retries_on_429_then_succeeds(client: MassiveClient) -> None:
    client.session = FakeSession([
        FakeResponse(429, {"status": "ERROR"}),
        FakeResponse(200, {"status": "OK"}),
    ])
    assert client.get("/v1/marketstatus/now")["status"] == "OK"
    assert len(client.session.calls) == 2


def test_get_retries_on_5xx_and_network_error(client: MassiveClient) -> None:
    client.session = FakeSession([
        FakeResponse(500, {}),
        requests.ConnectionError("boom"),
        FakeResponse(200, {"status": "OK"}),
    ])
    assert client.get("/x")["status"] == "OK"
    assert len(client.session.calls) == 3


def test_get_raises_after_retries_exhausted(client: MassiveClient) -> None:
    client.session = FakeSession([FakeResponse(503, {})] * http_client.MAX_TRIES)
    with pytest.raises(requests.HTTPError):
        client.get("/x")
    assert len(client.session.calls) == http_client.MAX_TRIES


def test_no_backoff_after_the_final_attempt(
    client: MassiveClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last failure raises at once; no retry follows, so no sleep either."""
    sleeps: list[float] = []
    monkeypatch.setattr(http_client.time, "sleep", sleeps.append)
    client.session = FakeSession([FakeResponse(503, {})] * http_client.MAX_TRIES)
    with pytest.raises(requests.HTTPError):
        client.get("/x")
    assert sleeps == [
        http_client.BACKOFF_BASE_S * (2 ** i) for i in range(http_client.MAX_TRIES - 1)
    ]


def test_get_403_raises_permission_error_with_hint(client: MassiveClient) -> None:
    client.session = FakeSession([
        FakeResponse(403, {"status": "NOT_AUTHORIZED", "message": "not entitled"})
    ])
    with pytest.raises(PermissionError, match="NOT_AUTHORIZED"):
        client.get("/v3/quotes/O:SPY260918C00765000")
    assert len(client.session.calls) == 1  # 403 is never retried


def test_get_non_retryable_error_raises_immediately(client: MassiveClient) -> None:
    client.session = FakeSession([FakeResponse(404, {"status": "NOT_FOUND"})])
    with pytest.raises(requests.HTTPError):
        client.get("/x")
    assert len(client.session.calls) == 1


def test_paginate_reappends_api_key_to_next_url(client: MassiveClient) -> None:
    # next_url as delivered by the API: cursor present, apiKey absent.
    next_url = "https://api.polygon.io/v3/trades/O:X?cursor=YXA9MQ"
    client.session = FakeSession([
        FakeResponse(200, {"results": [{"a": 1}, {"a": 2}], "next_url": next_url}),
        FakeResponse(200, {"results": [{"a": 3}]}),
    ])
    items = list(client.paginate("/v3/trades/O:X", {"order": "asc"}, limit=1000))
    assert [i["a"] for i in items] == [1, 2, 3]
    first, second = client.session.calls[0]["url"], client.session.calls[1]["url"]
    assert f"apiKey={API_KEY}" in first and "limit=1000" in first
    assert second.startswith(next_url.split("?")[0])
    assert "cursor=YXA9MQ" in second
    assert f"apiKey={API_KEY}" in second  # re-appended: API omits it
