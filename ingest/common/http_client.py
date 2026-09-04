"""HTTP client for the Massive.com (ex-Polygon.io) REST API.

Behaviour verified against the live API (see SPEC.md):
  * Auth is an ``apiKey`` query parameter; ``next_url`` values do NOT include
    it, so pagination must re-append the key.
  * 403 responses carry ``{"status": "NOT_AUTHORIZED", ...}`` and mean the
    current plan tier is not entitled to the endpoint -> PermissionError.
  * No ``X-RateLimit-*`` headers exist. Requests pass through a shared
    :class:`~ingest.common.ratelimit.TokenBucket` (so concurrent jobs bound
    their *total* outbound rate) and 429/5xx/network errors are retried with
    exponential backoff.

The client is safe to share across threads: ``requests.Session`` handles
concurrent GETs, and the bucket serialises admission.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from ingest.common import ratelimit
from ingest.common.config import Settings
from ingest.common.ratelimit import TokenBucket, default_bucket


def redact(url: str) -> str:
    """Strip the apiKey from a URL before it reaches a log or an exception.

    Auth is a query parameter on every request, so any message that echoes the
    URL -- a retry warning, an exhausted-retry HTTPError, a 403 entitlement
    message -- writes the live credential into the run log. Observed: a 429 on
    /fed/v1 put the key into two files under logs/.
    """
    return _APIKEY_RE.sub(r"\1***", url)


MAX_TRIES = 6
BACKOFF_BASE_S = 1.0
TIMEOUT_S = 30
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_APIKEY_RE = re.compile(r"(apiKey=)[^&\s]*", re.IGNORECASE)

log = logging.getLogger(__name__)


class MassiveHTTPError(requests.HTTPError):
    """HTTP failure with the status preserved and the apiKey stripped.

    Callers need the status to tell a quota (429) from a permanent 4xx: the
    rates backfill treats an exhausted 429 as a resumable stopping point and
    must not extend that leniency to a broken endpoint.
    """

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code} for {redact(url)}")


class MassiveClient:
    """Thin wrapper around ``requests.Session`` with auth, retries, pagination."""

    def __init__(
        self,
        settings: Settings,
        bucket: TokenBucket | None = None,
        priority: str = ratelimit.NORMAL,
    ) -> None:
        self.settings = settings
        self.base = settings.massive_api_base.rstrip("/")
        self.session = requests.Session()
        # Shared by default so every job on the box draws on one budget.
        self.bucket = bucket if bucket is not None else default_bucket()
        # Jobs whose data can be re-pulled from flat files pass
        # ``priority=ratelimit.LOW`` and stop drawing at the reserve, leaving
        # headroom for the snapshot sweep, which cannot be re-run.
        self.priority = priority

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

        Admission is gated by the shared token bucket, then 429/5xx/network
        errors are retried with exponential backoff (1s base, x2, up to
        ``MAX_TRIES`` attempts, 30s timeout).

        Raises:
            PermissionError: on HTTP 403 (tier not entitled to this endpoint).
            requests.HTTPError: on other non-retryable HTTP errors.
            requests.RequestException: when retries are exhausted.
        """
        url = self._with_api_key(self._url(path), params)
        last_exc: Exception | None = None
        for attempt in range(1, MAX_TRIES + 1):
            self.bucket.acquire(priority=self.priority)
            try:
                resp = self.session.get(url, timeout=TIMEOUT_S)
            except requests.RequestException as exc:  # network-level failure
                # str(exc) can embed the prepared URL, and therefore the
                # apiKey; redact before it reaches a log or is re-raised.
                detail = redact(f"{type(exc).__name__}: {exc}")
                last_exc = requests.RequestException(detail)
                self._sleep(attempt, f"network error: {detail}")
                continue
            if resp.status_code == 403:
                raise PermissionError(self._entitlement_message(url, resp))
            if resp.status_code in RETRYABLE_STATUS:
                last_exc = MassiveHTTPError(resp.status_code, url)
                self._sleep(attempt, f"HTTP {resp.status_code}")
                continue
            if resp.status_code >= 400:
                # The stdlib helper builds its message from the prepared URL,
                # which carries the apiKey. Raise our own instead.
                raise MassiveHTTPError(resp.status_code, url)
            return resp.json()  # type: ignore[no-any-return]
        assert last_exc is not None
        raise last_exc

    def _sleep(self, attempt: int, why: str) -> None:  # noqa: D401
        """Exponential backoff before retry ``attempt`` (base 1s, x2).

        Nothing follows the final attempt, so it skips the backoff: sleeping
        2**(MAX_TRIES-1) seconds before raising an error that is already
        decided only delays the caller's own retry/fail path (a 32s stall
        against snapshot_sweep's 60s budget).
        """
        if attempt >= MAX_TRIES:
            return
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
            f"403 NOT_AUTHORIZED from {redact(url)} (status={status} {detail}). "
            "Your Massive.com plan tier is not entitled to this endpoint "
            "(e.g. quotes / indices / equity trades are above the current tier)."
        )

    # ------------------------------------------------------------- paginate
    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        limit: int = 1000,
    ) -> Iterator[dict[str, Any]]:
        """Yield every item of ``results`` across all pages of a list endpoint.

        Follows ``next_url``, re-appending ``apiKey`` each time (the API does
        not include it). ``limit`` is merged into the first request's params.
        """
        merged: dict[str, Any] | None = dict(params or {})
        merged.setdefault("limit", limit)
        url = self._url(path)
        while True:
            body = self.get(url, merged)
            yield from body.get("results") or []
            next_url = body.get("next_url")
            if not next_url:
                return
            url, merged = next_url, None
