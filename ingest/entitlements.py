"""Assert the plan's entitlement map still holds, and that payloads parse.

Probing beats remembering. This module encodes what the "Options Developer"
tier actually returned when probed on 2026-08-31 and re-checks it, so that a
plan change, a vendor migration, or a quiet downgrade shows up as one red
build instead of months of 403s nobody reads. It also carries the payload
shape checks from the retired ``ingest.smoke``: an endpoint that answers 200
with a malformed body is a failure too.

Two failure directions matter and both are reported:
  * ``entitled`` endpoints that stop working -- data silently stops landing.
  * ``forbidden`` endpoints that start working -- there is new data available
    that no job is capturing yet.

Run: ``venv/bin/python -m ingest.entitlements``  (exit 1 on any mismatch)
"""

from __future__ import annotations

import sys
from collections.abc import Callable

import requests

from ingest import schemas
from ingest.common import market_gate
from ingest.common.config import Settings
from ingest.common.s3 import s3_client

ENTITLED, FORBIDDEN = "entitled", "forbidden"

_PLACEHOLDER = "REPLACE_ME"

# Body validators: a 200 is not enough -- the payload must still have the
# shape the jobs depend on. Each returns a short detail string and raises
# RuntimeError on a malformed body.
Validator = Callable[[dict], str]


def _v_marketstatus(body: dict) -> str:
    market = body.get("market")
    if not market:
        raise RuntimeError(f"unexpected marketstatus payload: {str(body)[:120]}")
    return f"market={market}"


def _v_contracts(body: dict) -> str:
    results = body.get("results") or []
    if not results:
        raise RuntimeError("contracts returned no results")
    required = {"ticker", "underlying_ticker", "contract_type", "expiration_date", "strike_price"}
    missing = required - set(results[0])
    if missing:
        raise RuntimeError(f"contract record missing fields: {sorted(missing)}")
    return f"first={results[0].get('ticker')}"


def _v_snapshot(body: dict) -> str:
    results = body.get("results") or []
    if not results:
        raise RuntimeError("snapshot returned no results")
    flat = schemas.flatten_snapshot(results[0])
    if not flat.get("ticker") or "day_close" not in flat:
        raise RuntimeError(f"flattened snapshot malformed: {flat}")
    return f"first={flat['ticker']}"


def _v_minute_aggs(body: dict) -> str:
    results = body.get("results") or []
    if not results:
        raise RuntimeError("no minute bars")
    bar = results[0]
    if not {"t", "o", "h", "l", "c", "v"} <= set(bar):
        raise RuntimeError(f"agg bar missing fields: {bar}")
    return f"{len(results)} bars"


# (name, path_template, expectation, validator). {d} is the last completed
# trading day. Validators run on entitled 200s only.
PROBES: list[tuple[str, str, str, Validator | None]] = [
    # --- entitled: these back the jobs we run --------------------------------
    ("marketstatus/now",      "/v1/marketstatus/now", ENTITLED, _v_marketstatus),
    ("marketstatus/upcoming", "/v1/marketstatus/upcoming", ENTITLED, None),
    ("reference/contracts",   "/v3/reference/options/contracts?underlying_ticker=SPY&limit=1", ENTITLED, _v_contracts),
    ("snapshot/options SPY",  "/v3/snapshot/options/SPY?limit=1", ENTITLED, _v_snapshot),
    ("snapshot/options SPX",  "/v3/snapshot/options/I:SPX?limit=1", ENTITLED, None),
    ("trades/option",         "/v3/trades/O:SPY260918C00770000?limit=1", ENTITLED, None),
    ("aggs/option minute T-1", "/v2/aggs/ticker/O:SPY260918C00770000/range/1/minute/{d}/{d}?limit=1", ENTITLED, _v_minute_aggs),
    ("aggs/equity minute T-1", "/v2/aggs/ticker/SPY/range/1/minute/{d}/{d}?limit=1", ENTITLED, _v_minute_aggs),
    ("aggs/grouped stocks",   "/v2/aggs/grouped/locale/us/market/stocks/{d}?limit=1", ENTITLED, None),
    ("reference/dividends",   "/v3/reference/dividends?ticker=SPY&limit=1", ENTITLED, None),

    # --- not entitled: do not build jobs against these -----------------------
    # Options NBBO. The single biggest structural gap: with no bid/ask, the
    # 1-minute snapshot sweep is the only record of option pricing state.
    ("quotes/option NBBO",    "/v3/quotes/O:SPY260918C00770000?limit=1", FORBIDDEN, None),
    ("last/nbbo option",      "/v2/last/nbbo/O:SPY260918C00770000", FORBIDDEN, None),
    # Index levels. I:SPX is recovered from put-call parity instead; see
    # ingest.jobs.forward_from_parity.
    ("aggs/index SPX",        "/v2/aggs/ticker/I:SPX/range/1/minute/{d}/{d}?limit=1", FORBIDDEN, None),
    ("aggs/index VIX",        "/v2/aggs/ticker/I:VIX/range/1/day/{d}/{d}?limit=1", FORBIDDEN, None),
    ("snapshot/indices",      "/v3/snapshot/indices?ticker.any_of=I:SPX,I:VIX", FORBIDDEN, None),
    # Equity tape.
    ("quotes/equity",         "/v3/quotes/SPY?limit=1", FORBIDDEN, None),
    ("trades/equity",         "/v3/trades/SPY?limit=1", FORBIDDEN, None),
    ("indicators/sma",        "/v1/indicators/sma/I:SPX?timespan=day&limit=1", FORBIDDEN, None),
]

# Flat-file prefixes: (name, key_template, expectation)
S3_PROBES: list[tuple[str, str, str]] = [
    ("flat trades_v1",      "us_options_opra/trades_v1/{y}/{m}/{d}.csv.gz", ENTITLED),
    ("flat minute_aggs_v1", "us_options_opra/minute_aggs_v1/{y}/{m}/{d}.csv.gz", ENTITLED),
    ("flat day_aggs_v1",    "us_options_opra/day_aggs_v1/{y}/{m}/{d}.csv.gz", ENTITLED),
    # ~131 GB/day even if it were entitled.
    ("flat quotes_v1",      "us_options_opra/quotes_v1/{y}/{m}/{d}.csv.gz", FORBIDDEN),
    ("flat us_indices",     "us_indices/minute_aggs_v1/{y}/{m}/{d}.csv.gz", FORBIDDEN),
    ("flat us_stocks_sip",  "us_stocks_sip/minute_aggs_v1/{y}/{m}/{d}.csv.gz", FORBIDDEN),
]


def _observed_rest(
    settings: Settings, path: str, validate: Validator | None = None
) -> tuple[str, str]:
    """Probe one REST path; returns (observed, detail)."""
    url = (
        settings.massive_api_base.rstrip("/") + path
        + ("&" if "?" in path else "?") + "apiKey=" + settings.massive_api_key
    )
    try:
        resp = requests.get(url, timeout=30)
    except requests.RequestException as exc:
        return "error", f"{type(exc).__name__}"
    if resp.status_code == 403:
        return FORBIDDEN, "403"
    if resp.status_code == 429:
        return "error", "429 (rate limited; rerun)"
    if not resp.ok:
        return "error", str(resp.status_code)
    if validate is None:
        return ENTITLED, str(resp.status_code)
    try:
        return ENTITLED, validate(resp.json())
    except Exception as exc:  # noqa: BLE001 - a malformed 200 is a probe failure
        return "error", f"{type(exc).__name__}: {exc}"


def _observed_s3(settings: Settings, key: str) -> tuple[str, str]:
    """Probe one flat-file key with HEAD; returns (observed, detail)."""
    from botocore.exceptions import ClientError

    if not settings.massive_s3_access_key_id:
        return "skip", "no S3 credentials"
    try:
        client = s3_client(settings)
    except ImportError:  # pragma: no cover
        return "skip", "boto3 unavailable"
    try:
        client.head_object(Bucket=settings.massive_s3_bucket, Key=key)
        return ENTITLED, "200"
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == 403:
            return FORBIDDEN, "403"
        if status == 404:
            return "error", "404 (no file for this date)"
        return "error", str(status)
    except Exception as exc:  # noqa: BLE001
        return "error", type(exc).__name__


def main(argv: list[str] | None = None) -> int:
    """Probe every documented entitlement; exit 1 on any mismatch."""
    settings = Settings.load()
    if not settings.massive_api_key or settings.massive_api_key.startswith(_PLACEHOLDER):
        print("MASSIVE_API_KEY missing or placeholder -- fix .env", file=sys.stderr)
        return 1
    d = market_gate.previous_trading_day(market_gate.today_et())
    # Flat files publish ~11:00 ET the next morning; step back one more day so
    # a morning run does not report a not-yet-published file as a mismatch.
    fd = market_gate.previous_trading_day(d)

    rows: list[tuple[str, str, str, str, str]] = []
    for name, template, expected, validate in PROBES:
        observed, detail = _observed_rest(settings, template.format(d=d.isoformat()), validate)
        rows.append((name, expected, observed, detail, "REST"))
    for name, template, expected in S3_PROBES:
        key = template.format(y=fd.year, m=f"{fd.month:02d}", d=fd.isoformat())
        observed, detail = _observed_s3(settings, key)
        rows.append((name, expected, observed, detail, "S3"))

    width = max(len(r[0]) for r in rows)
    print(f"entitlements -- REST date {d}, flat-file date {fd}")
    print("-" * (width + 46))
    mismatches = []
    for name, expected, observed, detail, kind in rows:
        if observed == "skip":
            status = "SKIP"
        elif observed == expected:
            status = "PASS"
        else:
            status = "FAIL"
            mismatches.append((name, expected, observed, detail))
        print(f"{status:<5} {name:<{width}}  {kind:<4} expect={expected:<9} got={observed} ({detail})")
    print("-" * (width + 46))

    if not mismatches:
        print(f"OK: all {len(rows)} entitlement expectations hold")
        return 0
    print(f"MISMATCHES ({len(mismatches)}):")
    for name, expected, observed, detail in mismatches:
        if expected == ENTITLED:
            print(f"  ! {name}: was entitled, now {observed} ({detail})"
                  " -- a job may have silently stopped landing data")
        else:
            print(f"  + {name}: was forbidden, now {observed} ({detail})"
                  " -- newly available data that nothing is capturing")
    return 1


if __name__ == "__main__":
    sys.exit(main())
