"""Glue from marketdata Contract/Quote/Forward to pricing scalars.

Single-quote helpers (:func:`price_quote`, :func:`greeks_quote`,
:func:`implied_vol_quote`) read spot, strike, expiry, call/put, and a parity
forward. They never read vendor greeks or vendor implied volatility.

:func:`greeks_asof` is the warehouse consumer: as-of snapshot → own IV → own
Greeks. Vendor ``greeks_*`` / ``implied_volatility`` are copied onto the
result as diagnostics and signed diffs (own − vendor). They are not inputs.

Two things here are load-bearing on this data feed:

* **Expiry is an instant.** Settlement is 16:00 ET for SPY/SPXW and 09:30 ET
  for AM-settled SPX (:data:`marketdata.opra.SETTLEMENT_ET`). Using the expiry
  *date* at UTC midnight is 20:00 ET the day before, understating T at every
  tenor and biasing inverted IV by ~108bp at 7 DTE.
* **SPX has no spot.** The index level is not entitled on this tier and the
  snapshot carries ``underlying_price = null`` for the whole SPX chain (~68% of
  the universe; SPXW alone is ~98% of SPX trade volume). Pass the per-expiry
  parity ``Forward`` and pricing switches to Black-76, which is the right model
  for a European index option anyway -- no dividend yield to guess.

Market price rule
-----------------
This warehouse is not entitled to option NBBO (no bid/ask on
``option_snapshots``). Invert ``last_trade_price`` when it is present, else
``day_close`` (see :attr:`marketdata.types.Quote.market_price`). A missing
price is skipped (nothing to invert). A price outside discounted no-arbitrage
bounds raises ``ValueError`` — never NaN.

Greeks engine per root
----------------------
SPX/SPXW → European BSM. SPY → American CRR (same invert is European BSM;
there is no American IV solver in :mod:`pricing.iv`). Chain CRR uses
``crr_steps`` (default 51, vs 401 on the single-quote engine) so a short
ATM slice is tractable. Pass ``spy_american_moneyness`` to CRR-price only
SPY strikes within that fraction of the forward and European-price the rest.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from marketdata.catalog import CatalogError, SchemaError, read_asof
from marketdata.opra import ALLOWED_ROOTS, settlement_time_et
from marketdata.types import (
    Contract,
    Forward,
    Quote,
    forward_from_record,
    quotes_from_snapshot_rows,
)
from marketdata.validate import narrow_roots, validate_table
from pricing.bsm import CallPut, resolve_q
from pricing.conventions import DEFAULT_CONVENTIONS, GreeksCatalog, GreeksConventions
from pricing.engine import AmericanCRR, Engine, EuropeanBSM
from pricing.iv import implied_vol as invert_iv

ET = ZoneInfo("America/New_York")

# Engines are chosen by the contract's own exercise style, not by a module
# default. SPY options are American; valuing them as European silently drops
# the early-exercise premium, which is exactly the kind of quiet modelling
# error this package exists to avoid. Callers can still pass `engine=` to
# override (e.g. to price SPY European deliberately, for comparison).
_ENGINES: dict[str, Engine] = {
    "european": EuropeanBSM(),
    "american": AmericanCRR(),
}

# CRR bump-and-revalue at 401 steps is the single-quote default; a full SPY
# chain at that depth is not a reasonable CLI. 51 steps stays American.
CHAIN_CRR_STEPS = 51

Uninvertible = Literal["raise", "skip"]


class ChainError(ValueError):
    """Fail-loud error proving warehouse → own IV → own Greeks."""


@dataclass
class ChainCounts:
    """Skip / priced tallies from one :func:`greeks_asof` pass.

    Expired (T≤0), missing last/close, outside no-arbitrage bounds, and
    (when ``moneyness`` is set) far-OTM rows are omitted from the table
    rather than written as NaN. The daily drift job reports these counts.
    """

    n_quotes: int = 0
    n_priced: int = 0
    n_expired: int = 0
    n_no_price: int = 0
    n_uninvertible: int = 0
    n_otm: int = 0


def engine_for(contract: Contract) -> Engine:
    """Default pricing engine for a contract's exercise style."""
    try:
        return _ENGINES[contract.exercise_style]
    except KeyError:
        raise ValueError(
            f"no engine for exercise_style {contract.exercise_style!r}; "
            f"known: {sorted(_ENGINES)}"
        ) from None


def expiry_instant(contract: Contract) -> datetime:
    """The moment ``contract`` settles, as an aware UTC datetime.

    16:00 ET for SPY and SPXW; 09:30 ET for AM-settled SPX.
    """
    hour, minute = settlement_time_et(contract.root)
    local = datetime(
        contract.expiry.year, contract.expiry.month, contract.expiry.day,
        hour, minute, tzinfo=ET,
    )
    return local.astimezone(UTC)


def year_fraction(contract: Contract, asof_ns: int, *, days: int = 365) -> float:
    """ACT/365 year fraction from the as-of instant to the settlement instant."""
    expiry_ns = expiry_instant(contract).timestamp() * 1e9
    t = (expiry_ns - asof_ns) / (days * 86400.0 * 1e9)
    if t <= 0:
        raise ValueError(
            f"non-positive T: {contract.ticker or contract.root} settles "
            f"{expiry_instant(contract).isoformat()}, asof_ns={asof_ns}"
        )
    return t


def price_quote(
    quote: Quote,
    *,
    r: float,
    sigma: float,
    q: float | None = None,
    forward: Forward | None = None,
    engine: Engine | None = None,
) -> float:
    """Price using quote.underlying_price and contract fields — not vendor IV."""
    S, T, cp, F = _spot_t_cp(quote, forward, r)
    eng = engine or engine_for(quote.contract)
    return float(eng.price(S, quote.contract.strike, T, r, sigma, cp, q=q, F=F))


def greeks_quote(
    quote: Quote,
    *,
    r: float,
    sigma: float,
    q: float | None = None,
    forward: Forward | None = None,
    engine: Engine | None = None,
    conventions: GreeksConventions = DEFAULT_CONVENTIONS,
) -> GreeksCatalog:
    """Greeks from market spot and our σ. Vendor greeks on the quote are ignored."""
    S, T, cp, F = _spot_t_cp(quote, forward, r)
    eng = engine or engine_for(quote.contract)
    return eng.greeks(S, quote.contract.strike, T, r, sigma, cp, q=q, F=F, conventions=conventions)


def implied_vol_quote(
    quote: Quote,
    *,
    r: float,
    q: float | None = None,
    forward: Forward | None = None,
) -> float:
    """Invert the quote's last/close; ignore the vendor IV diagnostic."""
    px = quote.market_price
    if px is None:
        raise ValueError("quote has no last or day_close to invert")
    S, T, cp, F = _spot_t_cp(quote, forward, r)
    return invert_iv(px, S, quote.contract.strike, T, r, cp, q=q, F=F)


def _spot_t_cp(
    quote: Quote, forward: Forward | None, r: float
) -> tuple[float, float, CallPut, float | None]:
    """Resolve ``(S, T, call_put, F)`` for one quote.

    When the snapshot carries no ``underlying_price`` -- which is the entire
    SPX chain on this tier -- fall back to the parity forward and price in
    Black-76 terms: ``S = F e^{-rT}`` with ``q = 0`` makes
    ``d1 = (ln(F/K) + sigma^2 T/2)/(sigma sqrt(T))`` and
    ``price = e^{-rT}[F N(d1) - K N(d2)]`` fall out of the existing BSM code
    with no new maths. Delta is then with respect to that synthetic spot.
    """
    if quote.asof_ns is None:
        raise ValueError("quote has no asof_ns")
    T = year_fraction(quote.contract, quote.asof_ns)
    cp: CallPut = quote.contract.call_put

    if quote.underlying_price is not None:
        S = float(quote.underlying_price)
        return S, T, cp, (None if forward is None else float(forward.forward))

    if forward is None:
        raise ValueError(
            f"{quote.contract.ticker or quote.contract.root}: snapshot has no "
            "underlying_price (expected for SPX -- the index level is not "
            "entitled on this tier) and no parity forward was supplied; pass "
            "the matching forwards row"
        )
    if forward.expiry != quote.contract.expiry:
        raise ValueError(
            f"forward expiry {forward.expiry} does not match contract expiry "
            f"{quote.contract.expiry}"
        )
    # Black-76 via the synthetic spot; q resolves to 0 inside resolve_q.
    return float(forward.forward) * math.exp(-r * T), T, cp, None


# ---------------------------------------------------------------------------
# As-of chain: warehouse snapshot + forwards → own IV → own Greeks
# ---------------------------------------------------------------------------

CHAIN_SCHEMA = pa.schema(
    [
        pa.field("ticker", pa.string()),
        pa.field("root", pa.string()),
        pa.field("underlying", pa.string()),
        pa.field("expiry", pa.string()),
        pa.field("call_put", pa.string()),
        pa.field("strike", pa.float64()),
        pa.field("exercise_style", pa.string()),
        pa.field("greeks_engine", pa.string()),
        pa.field("asof_ns", pa.int64()),
        pa.field("T", pa.float64()),
        pa.field("r", pa.float64()),
        pa.field("q", pa.float64()),
        pa.field("F", pa.float64()),
        pa.field("S", pa.float64()),
        pa.field("market_price", pa.float64()),
        pa.field("price_source", pa.string()),
        pa.field("own_iv", pa.float64()),
        pa.field("own_price", pa.float64()),
        pa.field("own_delta", pa.float64()),
        pa.field("own_gamma", pa.float64()),
        pa.field("own_theta", pa.float64()),
        pa.field("own_vega", pa.float64()),
        pa.field("vendor_iv", pa.float64()),
        pa.field("vendor_delta", pa.float64()),
        pa.field("vendor_gamma", pa.float64()),
        pa.field("vendor_theta", pa.float64()),
        pa.field("vendor_vega", pa.float64()),
        pa.field("diff_iv", pa.float64()),
        pa.field("diff_delta", pa.float64()),
        pa.field("diff_gamma", pa.float64()),
        pa.field("diff_theta", pa.float64()),
        pa.field("diff_vega", pa.float64()),
    ]
)


def canonical_underlying(label: str) -> str:
    """``I:SPX`` and ``SPX`` are the same underlier for joining forwards."""
    u = str(label or "").strip().upper()
    return u[2:] if u.startswith("I:") else u


def _finite(value: Any, name: str) -> float:
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"non-finite {name}: {value!r}")
    return out


def _opt_finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _signed_diff(own: float, vendor: float | None) -> float | None:
    if vendor is None:
        return None
    return own - vendor


def _price_source(quote: Quote) -> str:
    if quote.last is not None:
        return "last"
    return "close"


def _require_checks(checks: list[Any], *, what: str) -> None:
    failed = [c for c in checks if c.status == "FAIL"]
    if not failed:
        return
    detail = "; ".join(f"{c.name}: {c.detail}" for c in failed)
    raise ChainError(f"{what}: {detail}")


def index_forwards(table: pa.Table) -> dict[tuple[str, date], Forward]:
    """Map ``(canonical underlying, expiry)`` → Forward. Duplicates are errors."""
    out: dict[tuple[str, date], Forward] = {}
    for rec in table.to_pylist():
        fwd = forward_from_record(rec)
        root = canonical_underlying(fwd.underlying)
        if not root:
            raise ChainError("forwards row missing underlying_ticker")
        if not math.isfinite(fwd.forward) or fwd.forward <= 0:
            raise ChainError(
                f"invalid forward {fwd.forward!r} for {root} {fwd.expiry}"
            )
        key = (root, fwd.expiry)
        if key in out:
            raise ChainError(f"duplicate forward for {root} {fwd.expiry}")
        out[key] = fwd
    if not out:
        raise ChainError("no valid forwards rows")
    return out


def match_forward(
    contract: Contract, forwards: dict[tuple[str, date], Forward]
) -> Forward:
    key = (canonical_underlying(contract.underlying), contract.expiry)
    try:
        return forwards[key]
    except KeyError:
        raise ChainError(
            f"no forward for {contract.ticker or contract.root} "
            f"underlying={key[0]} expiry={key[1].isoformat()}"
        ) from None


def _chain_engine(
    contract: Contract,
    forward: Forward,
    *,
    crr_steps: int,
    spy_american_moneyness: float | None,
) -> Engine:
    if contract.exercise_style != "american":
        return EuropeanBSM()
    if spy_american_moneyness is not None:
        ref = float(forward.forward)
        if abs(contract.strike / ref - 1.0) > spy_american_moneyness:
            return EuropeanBSM()
    return AmericanCRR(n_steps=crr_steps)


def _vendor_diagnostics(quote: Quote) -> dict[str, float | None]:
    """Copy vendor IV/greeks for diffs. Not consumed by invert or Engine."""
    vendor_iv = _opt_finite(quote.vendor_implied_volatility)
    vendor_delta = _opt_finite(quote.vendor_delta)
    vendor_gamma = _opt_finite(quote.vendor_gamma)
    vendor_theta = _opt_finite(quote.vendor_theta)
    vendor_vega = _opt_finite(quote.vendor_vega)
    return {
        "vendor_iv": vendor_iv,
        "vendor_delta": vendor_delta,
        "vendor_gamma": vendor_gamma,
        "vendor_theta": vendor_theta,
        "vendor_vega": vendor_vega,
    }


def greeks_asof(
    dt: date,
    asof_ns: int | None = None,
    *,
    r: float,
    data_root: str | os.PathLike[str] | None = None,
    roots: tuple[str, ...] = ALLOWED_ROOTS,
    crr_steps: int = CHAIN_CRR_STEPS,
    spy_american_moneyness: float | None = None,
    moneyness: float | None = None,
    uninvertible: Uninvertible = "raise",
    max_rows: int | None = None,
    conventions: GreeksConventions = DEFAULT_CONVENTIONS,
    counts: ChainCounts | None = None,
) -> pa.Table:
    """Last snapshots and forwards at or before ``asof_ns`` → own IV and Greeks.

    Extra or missing parquet columns are :class:`~marketdata.catalog.SchemaError`.
    Foreign roots, mixed underlyings vs ``roots``, required-nulls, and an empty
    allowlisted root are :class:`ChainError`. A market price outside discounted
    bounds raises ``ValueError`` when ``uninvertible="raise"`` (never NaN).
    Expired (T≤0) rows and rows with no last/close are omitted; an empty
    result is an error. Pass ``counts`` to recover those skip tallies.

    ``moneyness`` (if set) skips strikes farther than that fraction of the
    parity forward so a cron CRR pass can stay on an ATM slice.
    """
    if not math.isfinite(r):
        raise ChainError(f"non-finite r: {r!r}")
    allow = narrow_roots(roots)
    if uninvertible not in ("raise", "skip"):
        raise ChainError(f"uninvertible must be raise/skip, got {uninvertible!r}")
    if crr_steps < 2:
        raise ChainError("crr_steps must be >= 2")
    if max_rows is not None and max_rows <= 0:
        raise ChainError("max_rows must be positive")
    if moneyness is not None and moneyness <= 0:
        raise ChainError("moneyness must be positive")

    snapshots = read_asof("option_snapshots", dt, asof_ns=asof_ns, data_root=data_root)
    forwards_table = read_asof("forwards", dt, asof_ns=asof_ns, data_root=data_root)
    _require_checks(
        validate_table(snapshots, "option_snapshots", roots=allow),
        what="option_snapshots",
    )
    _require_checks(
        validate_table(forwards_table, "forwards", roots=allow),
        what="forwards",
    )

    fwd_index = index_forwards(forwards_table)
    quotes = quotes_from_snapshot_rows(snapshots)
    rows: list[dict[str, Any]] = []
    n_expired = 0
    n_no_price = 0
    n_skipped = 0
    n_otm = 0

    for quote in quotes:
        if quote.contract.root not in allow:
            raise ChainError(
                f"foreign OPRA root {quote.contract.root!r} in "
                f"{quote.contract.ticker!r}; allowlist is {allow}"
            )
        priced_asof = asof_ns if asof_ns is not None else quote.asof_ns
        if priced_asof is None:
            raise ChainError(f"{quote.contract.ticker}: no asof_ns")
        qte = replace(quote, asof_ns=int(priced_asof))

        try:
            year_fraction(qte.contract, int(priced_asof))
        except ValueError:
            n_expired += 1
            continue

        if qte.market_price is None:
            n_no_price += 1
            continue

        try:
            fwd = match_forward(qte.contract, fwd_index)
        except ChainError:
            if uninvertible == "raise":
                raise
            n_skipped += 1
            continue

        if moneyness is not None:
            ref = float(fwd.forward)
            if ref <= 0 or abs(qte.contract.strike / ref - 1.0) > moneyness:
                n_otm += 1
                continue

        try:
            eng = _chain_engine(
                qte.contract,
                fwd,
                crr_steps=crr_steps,
                spy_american_moneyness=spy_american_moneyness,
            )
            own_iv = implied_vol_quote(qte, r=r, forward=fwd)
            if not math.isfinite(own_iv):
                raise ValueError(f"{qte.contract.ticker}: inverted IV is not finite")
            if own_iv <= 0:
                raise ValueError(
                    f"{qte.contract.ticker}: inverted IV is {own_iv}; "
                    "engines require sigma > 0 (price at the intrinsic bound)"
                )
            catalog = greeks_quote(
                qte, r=r, sigma=own_iv, forward=fwd, engine=eng, conventions=conventions
            )
            S, T, _cp, F_passed = _spot_t_cp(qte, fwd, r)
            q_out = (
                float(resolve_q(S, T, r, F=F_passed))
                if F_passed is not None
                else 0.0
            )
            own_price = _finite(catalog.price, "own_price")
            own_delta = _finite(catalog.delta, "own_delta")
            own_gamma = _finite(catalog.gamma, "own_gamma")
            own_theta = _finite(catalog.theta, "own_theta")
            own_vega = _finite(catalog.vega, "own_vega")
        except ChainError:
            raise
        except ValueError:
            if uninvertible == "raise":
                raise
            n_skipped += 1
            continue

        vendor = _vendor_diagnostics(qte)
        rows.append(
            {
                "ticker": qte.contract.ticker,
                "root": qte.contract.root,
                "underlying": qte.contract.underlying,
                "expiry": qte.contract.expiry.isoformat(),
                "call_put": qte.contract.call_put,
                "strike": float(qte.contract.strike),
                "exercise_style": qte.contract.exercise_style,
                "greeks_engine": eng.name,
                "asof_ns": int(qte.asof_ns),
                "T": float(T),
                "r": float(r),
                "q": float(q_out),
                "F": float(fwd.forward),
                "S": float(S),
                "market_price": float(qte.market_price),
                "price_source": _price_source(qte),
                "own_iv": float(own_iv),
                "own_price": own_price,
                "own_delta": own_delta,
                "own_gamma": own_gamma,
                "own_theta": own_theta,
                "own_vega": own_vega,
                "vendor_iv": vendor["vendor_iv"],
                "vendor_delta": vendor["vendor_delta"],
                "vendor_gamma": vendor["vendor_gamma"],
                "vendor_theta": vendor["vendor_theta"],
                "vendor_vega": vendor["vendor_vega"],
                "diff_iv": _signed_diff(float(own_iv), vendor["vendor_iv"]),
                "diff_delta": _signed_diff(own_delta, vendor["vendor_delta"]),
                "diff_gamma": _signed_diff(own_gamma, vendor["vendor_gamma"]),
                "diff_theta": _signed_diff(own_theta, vendor["vendor_theta"]),
                "diff_vega": _signed_diff(own_vega, vendor["vendor_vega"]),
            }
        )
        if max_rows is not None and len(rows) >= max_rows:
            break

    if counts is not None:
        counts.n_quotes = len(quotes)
        counts.n_priced = len(rows)
        counts.n_expired = n_expired
        counts.n_no_price = n_no_price
        counts.n_uninvertible = n_skipped
        counts.n_otm = n_otm

    if not rows:
        raise ChainError(
            "no contracts priced "
            f"(expired={n_expired} no_price={n_no_price} "
            f"uninvertible={n_skipped} otm={n_otm})"
        )
    return pa.Table.from_pylist(rows, schema=CHAIN_SCHEMA)


def main(argv: list[str] | None = None) -> int:
    """CLI: compute own IV and Greeks as-of a calendar date. Exit 1 on failure."""
    parser = argparse.ArgumentParser(
        prog="python -m pricing.from_market",
        description=(
            "Prove warehouse → own IV → own Greeks on SPY/SPX/SPXW. "
            "Vendor snapshot greeks/IV are diagnostics (own − vendor diffs), "
            "never pricing inputs. Market price is last_trade_price else "
            "day_close (no NBBO on this tier)."
        ),
    )
    parser.add_argument("--date", required=True, help="partition date YYYY-MM-DD")
    parser.add_argument(
        "--asof-ns",
        type=int,
        default=None,
        help="as-of instant (ns epoch); default = latest file per underlying",
    )
    parser.add_argument(
        "--r",
        type=float,
        required=True,
        help="continuous risk-free rate (no rates warehouse on this feed)",
    )
    parser.add_argument(
        "--roots",
        default=",".join(ALLOWED_ROOTS),
        help="comma-separated OPRA roots (default SPY,SPX,SPXW)",
    )
    parser.add_argument(
        "--data-root",
        default=os.environ.get("DATA_ROOT", "/data/massive"),
    )
    parser.add_argument(
        "--output",
        default=None,
        help="write the result parquet here (PyArrow, no pandas)",
    )
    parser.add_argument(
        "--crr-steps",
        type=int,
        default=CHAIN_CRR_STEPS,
        help=f"CRR steps for American SPY (default {CHAIN_CRR_STEPS})",
    )
    parser.add_argument(
        "--spy-atm-pct",
        type=float,
        default=None,
        help=(
            "if set, American CRR only for SPY strikes within this fraction "
            "of the forward; the rest of SPY is European BSM"
        ),
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="stop after this many priced rows (smoke / CRR budget)",
    )
    parser.add_argument(
        "--skip-uninvertible",
        action="store_true",
        help="omit rows whose last/close sits outside no-arbitrage bounds",
    )
    args = parser.parse_args(argv)

    try:
        roots = narrow_roots(tuple(args.roots.split(",")))
    except ValueError as exc:
        print(f"FAIL  roots  {exc}", file=sys.stderr)
        return 1

    dt = date.fromisoformat(args.date)
    uninvertible: Uninvertible = "skip" if args.skip_uninvertible else "raise"
    try:
        table = greeks_asof(
            dt,
            asof_ns=args.asof_ns,
            r=args.r,
            data_root=args.data_root,
            roots=roots,
            crr_steps=args.crr_steps,
            spy_american_moneyness=args.spy_atm_pct,
            uninvertible=uninvertible,
            max_rows=args.max_rows,
        )
    except (CatalogError, SchemaError, ChainError, ValueError) as exc:
        print(f"FAIL  chain  {exc}", file=sys.stderr)
        return 1

    engines: dict[str, int] = {}
    for name in table["greeks_engine"].to_pylist():
        engines[str(name)] = engines.get(str(name), 0) + 1
    print(
        f"PASS  rows={table.num_rows}  engines={engines}  "
        f"date={dt.isoformat()}  asof_ns={args.asof_ns}",
        file=sys.stderr,
    )
    if args.output:
        out = Path(args.output)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table, out)
        except OSError as exc:
            print(f"FAIL  output  {exc}", file=sys.stderr)
            return 1
        print(f"PASS  wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
