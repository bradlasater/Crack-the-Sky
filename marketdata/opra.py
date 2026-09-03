"""OPRA ticker parse with an explicit root allowlist.

Polygon/Massive option tickers look like ``O:{root}{YYMMDD}{C|P}{strike}``
where strike is OCC thousandths (8 digits, ``420`` -> ``00420000``).

The root is delimited by the expiry digits. Anchoring there is what
separates ``O:SPXW...`` (an SPX weekly) from ``O:SPXL...`` (a Direxion 3x
ETF). A prefix match on ``O:SPY`` admits SPYL/SPYS/SPYG; this parser
does not.

Exercise style and multiplier live on :class:`~marketdata.types.Contract`
so analytics never scatter those constants.
"""

from __future__ import annotations

import re
from datetime import date

from marketdata.types import CallPut, Contract, ExerciseStyle

# Longest-first: SPXW must win against SPX, VIXW against VIX.
ALLOWED_ROOTS: tuple[str, ...] = ("SPY", "SPX", "SPXW", "VIX", "VIXW")
MULTIPLIER: int = 100

# Settlement time of day, ET, per root. Expiry is an instant, not a date: SPX
# is AM-settled off the opening prints, while SPY and the SPXW weeklies settle
# at the close. Treating expiry as UTC midnight (20:00 ET the day before)
# understates T by ~20h at every tenor, which biases inverted IV by ~108bp at
# 7 DTE and ~15bp at 30 DTE, and makes every same-day expiry look expired.
SETTLEMENT_ET: dict[str, tuple[int, int]] = {
    "SPY": (16, 0),    # PM settled
    "SPXW": (16, 0),   # PM settled weeklies
    "SPX": (9, 30),    # AM settled monthlies
    # VIX options settle to the SOQ at the Wednesday OPEN -- both standard and
    # weekly series are AM settled, unlike the SPX/SPXW split above. Assuming
    # 16:00 here would reintroduce the ~20h T error on the whole VIX surface.
    "VIX": (9, 30),
    "VIXW": (9, 30),
}


def settlement_time_et(root: str) -> tuple[int, int]:
    """``(hour, minute)`` ET at which ``root`` settles on its expiry date."""
    try:
        return SETTLEMENT_ET[root]
    except KeyError:
        raise OPRAParseError(
            f"no settlement time for root {root!r}; known: {sorted(SETTLEMENT_ET)}"
        ) from None

_ROOT_META: dict[str, tuple[str, ExerciseStyle]] = {
    "SPY": ("SPY", "american"),
    "SPX": ("SPX", "european"),
    "SPXW": ("SPX", "european"),
    # VIX options are European options on the VIX FUTURE of that expiry, not
    # on the index. See forward_from_parity: the per-expiry parity forward is
    # the VX future, and must not be discounted to a "spot VIX".
    "VIX": ("VIX", "european"),
    "VIXW": ("VIX", "european"),
}

_TICKER_RE = re.compile(
    r"^O:(?P<root>SPXW|SPY|SPX|VIXW|VIX)"
    r"(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
    r"(?P<cp>[CP])"
    r"(?P<strike>\d{8})$"
)
_ANY_ROOT_RE = re.compile(r"^O:([A-Z]+)\d{6}[CP]\d+$")


class OPRAParseError(ValueError):
    """Raised when a ticker is not a well-formed allowlisted OPRA symbol."""


def ticker_root(ticker: str) -> str | None:
    """OPRA root of an option ticker (``O:SPXW26...`` -> ``SPXW``), else None.

    Unlike :func:`parse_opra` this does not enforce the allowlist; it is the
    hook validation uses to detect foreign roots (SPYL, SPXS, ...).
    """
    m = _ANY_ROOT_RE.match(str(ticker or ""))
    return m.group(1) if m else None


def parse_opra(ticker: str) -> Contract:
    """Parse ``O:{root}{YYMMDD}{C|P}{strike}`` into a :class:`Contract`.

    Raises:
        OPRAParseError: missing ``O:``, unknown root (including SPYL/SPXS/SPYG),
            malformed expiry, or missing strike. Never returns a partial.
    """
    if not isinstance(ticker, str) or not ticker:
        raise OPRAParseError(f"not an OPRA ticker: {ticker!r}")
    m = _TICKER_RE.match(ticker)
    if m is None:
        root = ticker_root(ticker)
        if root is not None and root not in ALLOWED_ROOTS:
            raise OPRAParseError(
                f"foreign OPRA root {root!r} in {ticker!r}; allowlist is {ALLOWED_ROOTS}"
            )
        raise OPRAParseError(f"not an allowlisted OPRA ticker: {ticker!r}")

    root = m.group("root")
    yy, mm, dd = int(m.group("yy")), int(m.group("mm")), int(m.group("dd"))
    year = 2000 + yy if yy < 80 else 1900 + yy
    try:
        expiry = date(year, mm, dd)
    except ValueError as exc:
        raise OPRAParseError(f"invalid OPRA expiry in {ticker!r}: {exc}") from exc

    cp_raw = m.group("cp")
    call_put: CallPut = "call" if cp_raw == "C" else "put"
    strike = int(m.group("strike")) / 1000.0
    underlying, style = _ROOT_META[root]
    return Contract(
        root=root,
        underlying=underlying,
        expiry=expiry,
        call_put=call_put,
        strike=strike,
        exercise_style=style,
        multiplier=MULTIPLIER,
        ticker=ticker,
    )
