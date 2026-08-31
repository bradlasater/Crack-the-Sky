"""smoke: six live checks against the Massive.com API and S3 flat files.

Run as ``python -m ingest.smoke``. Prints a PASS/FAIL/SKIP table and exits 0
when nothing FAILed. The S3 check reports SKIP (with a fix-creds hint) on
authentication failure rather than failing, because placeholder credentials
are a known deploy-time state.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import timedelta

from ingest import schemas
from ingest.common import market_gate
from ingest.common.config import Settings
from ingest.common.http_client import MassiveClient

_FIX_S3 = "fix S3 creds in .env (MASSIVE_S3_ACCESS_KEY_ID/SECRET from the Massive dashboard)"
_PLACEHOLDER = "REPLACE_ME"


class SkipCheck(Exception):
    """Raised by a check to report SKIP (environmental, not a code failure)."""


def check_config(settings: Settings, client: MassiveClient) -> str:
    """1) Settings load from .env / environment with a usable API key."""
    if not settings.massive_api_key or settings.massive_api_key.startswith(_PLACEHOLDER):
        raise RuntimeError("MASSIVE_API_KEY missing or placeholder")
    return f"base={settings.massive_api_base} data_root={settings.data_root}"


def check_marketstatus(settings: Settings, client: MassiveClient) -> str:
    """2) ``/v1/marketstatus/now`` responds with a market state."""
    body = client.get("/v1/marketstatus/now")
    market = body.get("market")
    if not market:
        raise RuntimeError(f"unexpected marketstatus payload: {str(body)[:120]}")
    return f"market={market} serverTime={body.get('serverTime')}"


def check_contracts(settings: Settings, client: MassiveClient) -> str:
    """3) Contracts endpoint returns records with the expected fields."""
    body = client.get(
        "/v3/reference/options/contracts",
        params={"underlying_ticker": "SPY", "limit": 5, "order": "asc", "sort": "ticker"},
    )
    results = body.get("results") or []
    if not results:
        raise RuntimeError("contracts returned no results")
    required = {"ticker", "underlying_ticker", "contract_type", "expiration_date", "strike_price"}
    missing = required - set(results[0])
    if missing:
        raise RuntimeError(f"contract record missing fields: {sorted(missing)}")
    return f"{len(results)} contracts, first={results[0].get('ticker')}"


def check_snapshot(settings: Settings, client: MassiveClient) -> str:
    """4) SPY chain snapshot (limit=3) parses through flatten_snapshot."""
    body = client.get("/v3/snapshot/options/SPY", params={"limit": 3})
    results = body.get("results") or []
    if not results:
        raise RuntimeError("snapshot returned no results")
    flat = schemas.flatten_snapshot(results[0])
    if not flat.get("ticker") or "day_close" not in flat:
        raise RuntimeError(f"flattened snapshot malformed: {flat}")
    return f"{len(results)} snapshots, first={flat['ticker']}"


def check_spy_minute_aggs(settings: Settings, client: MassiveClient) -> str:
    """5) SPY 1-minute aggs for the previous trading day parse."""
    day = market_gate.today_et() - timedelta(days=1)
    while not market_gate.is_trading_day(day, settings.data_root):
        day -= timedelta(days=1)
    body = client.get(
        f"/v2/aggs/ticker/SPY/range/1/minute/{day}/{day}",
        params={"adjusted": "true", "sort": "asc", "limit": 50000},
    )
    results = body.get("results") or []
    if not results:
        raise RuntimeError(f"no minute aggs for SPY on {day}")
    bar = results[0]
    if not {"t", "o", "h", "l", "c", "v"} <= set(bar):
        raise RuntimeError(f"agg bar missing fields: {bar}")
    return f"{len(results)} minute bars for SPY {day}"


def check_s3_head_bucket(settings: Settings, client: MassiveClient) -> str:
    """6) S3 head-bucket against the flat-files endpoint (SKIP on bad creds)."""
    ak, sk = settings.massive_s3_access_key_id, settings.massive_s3_secret_access_key
    if not ak or not sk or ak.startswith(_PLACEHOLDER) or sk.startswith(_PLACEHOLDER):
        raise SkipCheck(f"placeholder S3 credentials -- {_FIX_S3}")
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import ClientError
    except ImportError as exc:
        raise SkipCheck(f"boto3 not installed: {exc}") from exc
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.massive_s3_endpoint,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        config=Config(signature_version="s3v4"),
    )
    try:
        s3.head_bucket(Bucket=settings.massive_s3_bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "?")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", "?")
        if status == 403 or code in {"InvalidSignature", "SignatureDoesNotMatch", "AccessDenied"}:
            raise SkipCheck(f"S3 auth failed ({code}) -- {_FIX_S3}") from exc
        raise RuntimeError(f"head_bucket failed: {code} HTTP {status}") from exc
    return f"bucket {settings.massive_s3_bucket} reachable"


Check = Callable[[Settings, MassiveClient], str]


def main() -> int:
    """Run all smoke checks, print the table, exit 0 unless any check FAILs."""
    checks: list[tuple[str, Check]] = [
        ("config loads", check_config),
        ("marketstatus/now", check_marketstatus),
        ("contracts limit=5", check_contracts),
        ("snapshot SPY limit=3", check_snapshot),
        ("SPY minute aggs T-1", check_spy_minute_aggs),
        ("S3 head bucket", check_s3_head_bucket),
    ]
    try:
        settings = Settings.load()
    except SystemExit:
        # Settings.load exits 2 with a message when MASSIVE_API_KEY is missing.
        print(f"{'config loads':<22} FAIL   MASSIVE_API_KEY not set")
        return 1
    client = MassiveClient(settings)

    results: list[tuple[str, str, str]] = []
    for name, fn in checks:
        try:
            detail = fn(settings, client)
            results.append((name, "PASS", detail))
        except SkipCheck as exc:
            results.append((name, "SKIP", str(exc)))
        except Exception as exc:  # noqa: BLE001 - smoke must report, not crash
            results.append((name, "FAIL", f"{type(exc).__name__}: {exc}"))

    print("\nsmoke checks:")
    for name, status, detail in results:
        print(f"  {name:<22} {status:<5}  {detail}")
    n_fail = sum(1 for _, status, _ in results if status == "FAIL")
    n_skip = sum(1 for _, status, _ in results if status == "SKIP")
    print(f"\n{len(results) - n_fail - n_skip} PASS, {n_skip} SKIP, {n_fail} FAIL")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
