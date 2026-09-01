"""ibkr_executions: broker fills via the IBKR Flex Web Service.

No gateway, no TWS, no market-data subscription -- plain HTTPS in two steps:

    SendRequest(t=token, q=queryId, v=3)  -> ReferenceCode + Url
    GetStatement(t=token, q=ReferenceCode, v=3) -> the statement XML

The statement is generated asynchronously, so ``GetStatement`` answers with
error 1019 ("statement generation in progress") for the first few seconds; that
is expected and is polled, not an error.

Why this lives next to the market data: an execution is only interpretable
against the tape it happened in. Landing fills beside ``option_trades`` and
``option_snapshots`` means a fill can be placed in the chain -- what the
surface looked like, where the print sat in the day's volume -- which is the
whole point of collecting them here rather than reading them in a browser.

The vendor XML is landed verbatim under ``raw/`` and the projection under
``clean/``, exactly as flat files are handled: the vendor payload is the record
of truth and is never rewritten.

Run: ``python -m ingest.jobs.ibkr_executions [--date YYYY-MM-DD]``
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import requests

from ingest.common import landing, market_gate
from ingest.common.cli import run_job
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger

JOB = "ibkr_executions"
DATASET = "ibkr_executions"

SEND_PATH = "/SendRequest"
GET_PATH = "/GetStatement"
API_VERSION = "3"
TIMEOUT_S = 60

# Flex generates asynchronously; 1019 means "not ready yet".
IN_PROGRESS_CODES = {"1019"}
POLL_SLEEP_S = 5
POLL_TRIES = 12

# Codes worth naming in the error, because each has a different fix.
FATAL_HINTS = {
    "1012": "check IBKR_FLEX_TOKEN — regenerate it in Flex Web Service",
    "1014": "check IBKR_FLEX_QUERY_ID — it is the Flex Query's numeric id, "
            "not the token",
    # Observed live: IBKR returns 1015 for a token that is malformed as well as
    # one that has aged out, so do not assert which it is.
    "1015": "check IBKR_FLEX_TOKEN — invalid or expired; regenerate it "
            "(tokens last ~1 year)",
    "1020": "malformed request — check the token/query id are numeric",
}

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


class FlexError(RuntimeError):
    """The Flex service refused the request (never carries the token)."""


def _safe_url(url: str) -> str:
    """Path only -- the query string carries the token."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _get(url: str, params: dict[str, str]) -> str:
    """GET, never letting the token escape into an exception message.

    ``raise_for_status()`` puts the fully prepared URL -- including ``t=<token>``
    -- into the HTTPError, and ``run_job`` logs ``str(exc)`` AND posts it as the
    Healthchecks failure body, which leaves the box. Any request failure is
    therefore re-raised with the URL reduced to its path.
    """
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT_S)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise FlexError(f"HTTP {status} from {_safe_url(url)}") from None
    except requests.RequestException as exc:
        raise FlexError(f"{type(exc).__name__} contacting {_safe_url(url)}") from None
    return resp.text


def _fault(xml_text: str) -> tuple[str, str] | None:
    """``(code, message)`` when the response is a Flex error, else None."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ("?", xml_text[:200])
    if (root.findtext("Status") or "").strip().lower() == "fail":
        return (root.findtext("ErrorCode") or "?", root.findtext("ErrorMessage") or "")
    return None


def fetch_statement(settings: Settings, logger: JsonlLogger) -> str:
    """Run the two-step Flex exchange and return the statement XML."""
    if not settings.ibkr_flex_token:
        raise FlexError("IBKR_FLEX_TOKEN is not set in .env")
    if not settings.ibkr_flex_query_id:
        raise FlexError(
            "IBKR_FLEX_QUERY_ID is not set in .env — create an Activity Flex "
            "Query in Account Management and use its numeric id"
        )
    base = settings.ibkr_flex_base
    auth = {"t": settings.ibkr_flex_token, "v": API_VERSION}

    body = _get(base + SEND_PATH, {**auth, "q": settings.ibkr_flex_query_id})
    fault = _fault(body)
    if fault:
        code, msg = fault
        raise FlexError(f"SendRequest failed [{code}] {msg}"
                        + (f" — {FATAL_HINTS[code]}" if code in FATAL_HINTS else ""))
    root = ET.fromstring(body)
    ref = (root.findtext("ReferenceCode") or "").strip()
    url = (root.findtext("Url") or (base + GET_PATH)).strip()
    if not ref:
        raise FlexError(f"SendRequest returned no ReferenceCode: {body[:200]}")
    logger.log("flex_requested", reference_code=ref)

    for attempt in range(1, POLL_TRIES + 1):
        body = _get(url, {**auth, "q": ref})
        fault = _fault(body)
        if fault is None:
            logger.log("flex_ready", attempt=attempt)
            return body
        code, msg = fault
        if code not in IN_PROGRESS_CODES:
            raise FlexError(f"GetStatement failed [{code}] {msg}"
                            + (f" — {FATAL_HINTS[code]}" if code in FATAL_HINTS else ""))
        logger.log("flex_generating", attempt=attempt, retry_in_s=POLL_SLEEP_S)
        time.sleep(POLL_SLEEP_S)
    raise FlexError(
        f"statement still generating after {POLL_TRIES * POLL_SLEEP_S}s (ref {ref})"
    )


def _f(value: str | None) -> float | None:
    if value is None or value.strip() in ("", "-"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def opra_ticker(row: dict[str, str]) -> str | None:
    """Rebuild the OPRA symbol so a fill can be joined to the tape.

    IBKR reports the pieces (underlying, expiry, strike, right) rather than an
    OPRA symbol, and its ``symbol`` field is the IBKR local symbol. Producing
    ``O:{root}{YYMMDD}{C|P}{strike*1000}`` is what lets an execution be looked
    up in ``option_trades`` / ``option_snapshots``.
    """
    if (row.get("assetCategory") or "").upper() not in ("OPT", "FOP"):
        return None
    root = (row.get("underlyingSymbol") or "").strip().upper()
    right = (row.get("putCall") or "").strip().upper()[:1]
    strike = _f(row.get("strike"))
    expiry = (row.get("expiry") or "").strip()
    if not (root and right in ("C", "P") and strike and expiry):
        return None
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", expiry)
    if m:
        yy, mm, dd = m.group(1)[2:], m.group(2), m.group(3)
    else:
        m = re.fullmatch(r"(\d{2})([A-Z]{3})(\d{2})", expiry.upper())  # 16SEP26
        if not m:
            return None
        dd, mon, yy = m.group(1), m.group(2), m.group(3)
        mm = f"{_MONTHS.get(mon, 0):02d}"
        if mm == "00":
            return None
    return f"O:{root}{yy}{mm}{dd}{right}{int(round(strike * 1000)):08d}"


def _record(row: dict[str, str]) -> dict[str, Any]:
    """Map one Flex ``<Trade>`` element's attributes to the schema."""
    return {
        "account_id": row.get("accountId"),
        "trade_id": row.get("tradeID"),
        "exec_id": row.get("ibExecID") or row.get("execID"),
        "order_id": row.get("ibOrderID") or row.get("orderID"),
        "symbol": row.get("symbol"),
        "opra_ticker": opra_ticker(row),
        "underlying_symbol": row.get("underlyingSymbol"),
        "asset_class": row.get("assetCategory"),
        "put_call": row.get("putCall"),
        "strike": _f(row.get("strike")),
        "expiry": row.get("expiry"),
        "multiplier": _f(row.get("multiplier")),
        "buy_sell": row.get("buySell"),
        "quantity": _f(row.get("quantity")),
        "trade_price": _f(row.get("tradePrice")),
        "trade_money": _f(row.get("tradeMoney")),
        "proceeds": _f(row.get("proceeds")),
        "commission": _f(row.get("ibCommission")),
        "realized_pnl": _f(row.get("fifoPnlRealized")),
        "currency": row.get("currency"),
        "trade_date": row.get("tradeDate"),
        "trade_datetime": row.get("dateTime") or row.get("tradeTime"),
        "order_time": row.get("orderTime"),
        "open_close": row.get("openCloseIndicator"),
        "exchange": row.get("exchange"),
        "notes": row.get("notes") or row.get("notes/codes"),
    }


def statement_period(xml_text: str) -> tuple[str | None, str | None]:
    """``(fromDate, toDate)`` of the first FlexStatement, as YYYY-MM-DD."""
    def _iso(v: str | None) -> str | None:
        v = (v or "").strip()
        if len(v) == 8 and v.isdigit():
            return f"{v[:4]}-{v[4:6]}-{v[6:]}"
        return v or None

    root = ET.fromstring(xml_text)
    el = next(root.iter("FlexStatement"), None)
    if el is None:
        return (None, None)
    return _iso(el.attrib.get("fromDate")), _iso(el.attrib.get("toDate"))


def parse_trades(xml_text: str, account_id: str | None = None) -> list[dict[str, Any]]:
    """Every ``<Trade>`` in a Flex statement, optionally filtered by account."""
    root = ET.fromstring(xml_text)
    out: list[dict[str, Any]] = []
    for el in root.iter("Trade"):
        row = dict(el.attrib)
        if account_id and row.get("accountId") != account_id:
            # Exact match required, including when accountId is absent: account
            # isolation depends on this field, so a query configured without it
            # must not silently ingest rows whose account cannot be verified.
            continue
        out.append(_record(row))
    return out


def check_parsed(records: list[dict[str, Any]]) -> None:
    """Fail when rows arrived but the field names clearly did not match.

    Flex attribute spellings depend on how the query was configured, and
    ``dict.get`` on a name IBKR does not emit yields None rather than an
    error. Without this, a query missing (say) UnderlyingSymbol/Strike/Expiry
    lands a full set of rows whose every column is null, and the job reports
    success. These two checks are deliberately narrow -- they fire only when
    a whole class of fields is absent, never on one odd row.
    """
    if not records:
        return

    priced = [r for r in records if r["trade_price"] is not None
              or r["quantity"] is not None]
    if not priced:
        raise FlexError(
            f"parsed {len(records)} trade rows but every trade_price and "
            "quantity is null -- the Flex query is almost certainly missing "
            "TradePrice/Quantity, or emits different field names. Check the "
            "query's selected fields in Account Management."
        )

    options = [r for r in records if (r["asset_class"] or "").upper() == "OPT"]
    if options and not any(r["opra_ticker"] for r in options):
        raise FlexError(
            f"parsed {len(options)} option rows but rebuilt no OPRA ticker -- "
            "the query is missing UnderlyingSymbol, Expiry, Strike or "
            "Put/Call, so fills cannot be joined to option_trades. Add those "
            "fields to the Flex query."
        )


def _main_fn(args, settings: Settings, logger: JsonlLogger):
    requested = date.fromisoformat(args.date) if args.date else None
    xml_text = fetch_statement(settings, logger)

    # The Flex query carries its own saved period ("Last Business Day"), which
    # --date cannot change. Landing whatever came back under a requested date
    # would file today's statement as history. Trust the statement.
    from_date, to_date = statement_period(xml_text)
    logger.log("flex_period", from_date=from_date, to_date=to_date,
               requested=requested.isoformat() if requested else None)
    if requested is not None and to_date and requested.isoformat() != to_date:
        raise ValueError(
            f"--date {requested} does not match the statement period "
            f"{from_date}..{to_date}. The Flex query's saved period decides what "
            "is returned; change the period in Account Management (or make a "
            "second query) rather than relabelling this statement."
        )
    run_date = (
        date.fromisoformat(to_date) if to_date
        else requested or market_gate.previous_trading_day(market_gate.today_et())
    )
    records = parse_trades(xml_text, settings.ibkr_account_id)

    if args.limit is not None:
        records = records[: args.limit]

    matched = sum(1 for r in records if r["opra_ticker"])
    logger.log(
        "flex_parsed",
        rows=len(records),
        opra_resolved=matched,
        accounts=sorted({r["account_id"] for r in records if r["account_id"]}),
    )
    check_parsed(records)

    if not records:
        # An empty statement is normal on a day with no fills. Say so plainly
        # rather than failing, but land nothing.
        logger.log("flex_no_trades", date=run_date.isoformat())
        return {"rows": 0, "date": run_date.isoformat()}

    if not args.dry_run:
        raw_path = landing.write_raw_text(
            DATASET, run_date, xml_text, job=JOB, ext="xml",
            data_root=settings.data_root,
        )
        clean_path = landing.write_clean(
            DATASET, run_date, records, job=JOB, data_root=settings.data_root
        )
        logger.log("flex_written", rows=len(records),
                   raw_path=str(raw_path), clean_path=str(clean_path))
    return {"rows": len(records), "opra_resolved": matched,
            "date": run_date.isoformat()}


def main(argv: list[str] | None = None) -> None:
    """Entry point: ``python -m ingest.jobs.ibkr_executions``."""
    run_job(JOB, _main_fn, argv)


if __name__ == "__main__":
    main()
