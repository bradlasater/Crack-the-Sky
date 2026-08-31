"""HTTP client for the Massive.com (ex-Polygon.io) REST API.

Behaviour verified against the live API (see SPEC.md):
  * Auth is an ``apiKey`` query parameter; ``next_url`` values do NOT include
    it, so pagination must re-append the key.
  * 403 responses carry ``{"status": "NOT_AUTHORIZED", ...}`` and mean the
    current plan tier is not entitled to the endpoint -> PermissionError.
  * No ``X-RateLimit-*`` headers exist; we stay polite with plain sequential
    requests and only retry 429/5xx/network errors with exponential backoff.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from ingest.common.config import Settings

MAX_TRIES = 6
BACKOFF_BASE_S = 1.0
TIMEOUT_S = 30
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

log = logging.getLogger(__name__)


class MassiveClient:
    """Thin wrapper around ``requests.Session`` with auth, retries, pagination."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base = settings.massive_api_base.rstrip("/")
        self.session = requests.Session()

    # ------------------------------------------------------------------ utils
    def _url(self, path: str) -> str:
        """Return an absolute URL for ``path`` (already-absolute URLs pass through)."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base}/{path.lstrip('/')}"

    def _with_api_key(self, url: str, params: dict[str, Any] | None) -> str:
        """Return ``url`` with the apiKey query param (re-)appended.

        ``next_url`` from the API never contains the key, so callers must pass
        pagination URLs through here.
        """
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if params:
            query.update({k: str(v) for k, v in params.items() if v is not None})
        query["apiKey"] = self.settings.massive_api_key
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    # ------------------------------------------------------------------- GET
    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform a single GET and return the decoded JSON body.

        Retries 429/5xx/network errors with exponential backoff
        (1s base, x2, up to ``MAX_TRIES`` attempts, 30s timeout).

        Raises:
            PermissionError: on HTTP 403 (tier not entitled to this endpoint).
            requests.HTTPError: on other non-retryable HTTP errors.
            requests.RequestException: when retries are exhausted.
        """
        url = self._with_api_key(self._url(path), params)
        last_exc: Exception | None = None
        for attempt in range(1, MAX_TRIES + 1):
            try:
                resp = self.session.get(url, timeout=TIMEOUT_S)
            except requests.RequestException as exc:  # network-level failure
                last_exc = exc
                self._sleep(attempt, f"network error: {exc}")
                continue
            if resp.status_code == 403:
                raise PermissionError(self._entitlement_message(url, resp))
            if resp.status_code in RETRYABLE_STATUS:
                last_exc = requests.HTTPError(f"HTTP {resp.status_code} for {url}")
                self._sleep(attempt, f"HTTP {resp.status_code}")
                continue
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
        assert last_exc is not None
        raise last_exc

    def _sleep(self, attempt: int, why: str) -> None:
        """Exponential backoff before retry ``attempt`` (base 1s, x2)."""
        delay = BACKOFF_BASE_S * (2 ** (attempt - 1))
        log.warning("retry %d/%d in %.1fs (%s)", attempt, MAX_TRIES, delay, why)
        time.sleep(delay)

    @staticmethod
    def _entitlement_message(url: str, resp: requests.Response) -> str:
        try:
            body = resp.json()
            status = body.get("status", "?")
            detail = body.get("message") or body.get("error") or ""
        except ValueError:
            status, detail = "?", resp.text[:200]
        return (
            f"403 NOT_AUTHORIZED from {url} (status={status} {detail}). "
            "Your Massive.com plan tier is not entitled to this endpoint "
            "(e.g. quotes / indices / equity trades are above the current tier)."
        )

    # ------------------------------------------------------------- paginate
    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        limit: int = 1000,
        max_pages: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every item of ``results`` across all pages of a list endpoint.

        Follows ``next_url``, re-appending ``apiKey`` each time (the API does
        not include it). ``limit`` is merged into the first request's params;
        ``max_pages`` caps the number of pages fetched (None = no cap).
        """
        merged: dict[str, Any] | None = dict(params or {})
        merged.setdefault("limit", limit)
        url = self._url(path)
        page = 0
        while True:
            page += 1
            body = self.get(url, merged)
            for item in body.get("results") or []:
                yield item
            next_url = body.get("next_url")
            if not next_url or (max_pages is not None and page >= max_pages):
                return
            url, merged = next_url, None
