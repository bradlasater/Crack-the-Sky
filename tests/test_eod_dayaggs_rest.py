"""eod_dayaggs_rest: a 404 means the contract did not trade, not a failed run.

MassiveHTTPError carries ``status_code`` but never sets ``response`` (the
client builds it without one so the apiKey cannot leak through a response
body), so a skip check written against ``exc.response.status_code`` never
matched -- the first unknown/delisted contract re-raised and killed the whole
EOD sweep partway through.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from ingest.common.http_client import MassiveHTTPError
from ingest.jobs.eod_dayaggs_rest import _fetch_day_bar


class _Client:
    """Stands in for MassiveClient: scripted body or failure."""

    def __init__(self, body: dict | None = None, exc: Exception | None = None) -> None:
        self.body = body or {}
        self.exc = exc

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        if self.exc is not None:
            raise self.exc
        return self.body


def test_404_means_the_contract_did_not_trade() -> None:
    client = _Client(exc=MassiveHTTPError(404, "https://x?apiKey=SECRET"))
    assert _fetch_day_bar(client, "O:SPY260918C00765000", "2026-09-01") is None


def test_empty_results_are_an_empty_list_not_none() -> None:
    """200 with no bar is distinct from 404: both skipped, counted apart."""
    assert _fetch_day_bar(_Client(body={"results": []}), "T", "2026-09-01") == []


def test_other_http_errors_still_raise() -> None:
    client = _Client(exc=MassiveHTTPError(500, "https://x?apiKey=SECRET"))
    with pytest.raises(requests.HTTPError):
        _fetch_day_bar(client, "T", "2026-09-01")


def test_a_plain_http_error_carrying_a_response_still_matches() -> None:
    resp = requests.Response()
    resp.status_code = 404
    client = _Client(exc=requests.HTTPError("404", response=resp))
    assert _fetch_day_bar(client, "T", "2026-09-01") is None
