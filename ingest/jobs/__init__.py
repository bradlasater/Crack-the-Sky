"""REST batch ingestion jobs (``python -m ingest.jobs.<name>``).

This package init carries the small helpers shared by several jobs:
clean-partition readers, per-underlying reference prices, the watchlist
computation and CLI conveniences for job-specific extra flags on top of the
shared ``cli.run_job`` parser.

Reference prices are **per underlying**. An earlier version derived a single
strike band from the SPY price and applied it to every contract; because SPX
trades near 10x SPY, that band excluded essentially the entire SPX/SPXW
universe (on 2026-08-31 it selected 2 of 28,648 SPX contracts) while logging
a healthy-looking run. The index level itself (``I:SPX``) is not entitled on
this tier at any endpoint, so the SPX reference comes from put-call parity on
the option chain we already snapshot -- see :func:`forward_from_parity`.
"""

from __future__ import annotations

import argparse
import math
import re
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ingest.common import market_gate
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger

# Watchlist parameters: expiration 7-45 days out, strike within +/-15% of
# that underlying's own reference price, and some sign of life (traded today
# or a real open-interest position). The liquidity filter is strictly better
# than tightening the band: it keeps every contract that actually trades and
# drops dead strikes.
DTE_MIN = 7
DTE_MAX = 45
MONEYNESS_PCT = 0.15
MIN_OPEN_INTEREST = 100

DAY_MS = 86_400_000

# OPRA symbology is O:{root}{YYMMDD}{C|P}{strike*1000}; the root is delimited
# by the expiry digits. Anchoring on them is what separates O:SPXW... (an SPX
# weekly, ~98% of all SPX option trades) from O:SPXL... (a Direxion 3x ETF),
# and O:VIXW... (a VIX weekly) from O:VIXY... (the ProShares VIX ETF).
OPTION_ROOTS = ("SPY", "SPX", "SPXW", "VIX", "VIXW")
_OPTION_TICKER_RE = re.compile(r"^O:(" + "|".join(OPTION_ROOTS) + r")\d{6}[CP]\d+$")


def keep_ticker(ticker: str) -> bool:
    """True when ``ticker`` is an option on SPY or SPX (incl. SPX weeklies)."""
    return bool(_OPTION_TICKER_RE.match(ticker or ""))


def ticker_root(ticker: str) -> str | None:
    """OPRA root of an option ticker (``O:SPXW26...`` -> ``SPXW``), else None."""
    m = re.match(r"^O:([A-Z]+)\d{6}[CP]\d+$", str(ticker or ""))
    return m.group(1) if m else None


def run_date_from_args(args: argparse.Namespace) -> date:
    """Resolve the trading date from parsed CLI args (default: today, ET)."""
    return date.fromisoformat(args.date) if args.date else market_gate.today_et()


def strip_flag(argv: list[str], flag: str) -> tuple[list[str], bool]:
    """Remove a job-specific boolean ``flag`` from ``argv``; return (argv, present).

    The shared ``cli.run_job`` parser only knows the common flags, so jobs with
    extra switches (``--expired``, ``--eod``, ``--watchlist``) peel them off
    before handing the remaining argv to the runner.
    """
    present = flag in argv
    return [a for a in argv if a != flag], present


def parse_underlyings(raw: str | None, default: list[str]) -> list[str]:
    """Parse the ``--underlying`` CSV argument into a list of tickers."""
    if not raw:
        return list(default)
    return [u.strip() for u in raw.split(",") if u.strip()]


# ---------------------------------------------------------------------------
# Clean-partition readers
# ---------------------------------------------------------------------------

def _clean_root(settings: Settings, dataset: str) -> Path:
    return Path(settings.data_root) / "clean" / dataset


def partition_dates(settings: Settings, dataset: str) -> list[date]:
    """All ``dt=YYYY-MM-DD`` partition dates present for a clean dataset."""
    root = _clean_root(settings, dataset)
    out: list[date] = []
    if not root.is_dir():
        return out
    for child in root.iterdir():
        if child.is_dir() and child.name.startswith("dt="):
            try:
                out.append(date.fromisoformat(child.name[3:]))
            except ValueError:
                continue
    return sorted(out)


def latest_partition(
    settings: Settings, dataset: str, on_or_before: date
) -> date | None:
    """Most recent clean partition date for ``dataset`` at or before a date."""
    candidates = [d for d in partition_dates(settings, dataset) if d <= on_or_before]
    return candidates[-1] if candidates else None


def read_partition(settings: Settings, dataset: str, dt: date) -> list[dict[str, Any]]:
    """Read every parquet file of one clean partition into a list of dicts.

    Requires pyarrow; raises ImportError with a clear message otherwise.
    """
    from ingest import schemas

    if schemas.pa is None:
        raise ImportError(
            "pyarrow is required to read clean partitions; "
            "install it (pip install -r requirements.txt)"
        )
    import pyarrow.parquet as pq

    part_dir = _clean_root(settings, dataset) / f"dt={dt.isoformat()}"
    records: list[dict[str, Any]] = []
    for path in sorted(part_dir.glob("*.parquet")):
        records.extend(pq.read_table(path).to_pylist())
    return records


def latest_clean_records(
    settings: Settings, dataset: str, on_or_before: date
) -> list[dict[str, Any]]:
    """Records of the most recent clean partition at or before ``on_or_before``."""
    dt = latest_partition(settings, dataset, on_or_before)
    if dt is None:
        return []
    return read_partition(settings, dataset, dt)


# ---------------------------------------------------------------------------
# Underlyings and reference prices
# ---------------------------------------------------------------------------

def underlying_root(underlying: str) -> str:
    """Canonical root for an underlying label.

    ``contracts`` records carry ``SPX`` while ``option_snapshots`` carry
    ``I:SPX`` for the same universe; both normalise to ``SPX`` so the two
    datasets can be joined.
    """
    u = str(underlying).strip().upper()
    return u[2:] if u.startswith("I:") else u


def _latest_files_by_underlying(
    settings: Settings, dataset: str, dt: date
) -> tuple[dict[str, Path], list[Path]]:
    """Newest parquet per underlying root within one clean partition.

    Returns ``(by_root, unrecognised)``. Files written by the jobs are named
    ``{label}-{underlying}-{epoch_ms}.parquet``; anything not matching that
    shape is returned separately rather than dropped, so a hand-written or
    externally-imported partition is never silently ignored.

    Reading the whole partition is wrong for any dataset written more than
    once a day: ``contracts_sync`` runs twice (08:00 and 16:30) so a
    whole-partition read returns each contract twice, and at a 1-minute
    snapshot cadence a partition holds hundreds of sweeps of the same chain.
    """
    part = _clean_root(settings, dataset) / f"dt={dt.isoformat()}"
    newest: dict[str, tuple[int, Path]] = {}
    other: list[Path] = []
    for path in sorted(part.glob("*.parquet")):
        parts = path.stem.rsplit("-", 2)
        stamp: int | None = None
        if len(parts) == 3:
            try:
                stamp = int(parts[2])
            except ValueError:
                stamp = None
        if stamp is None:
            other.append(path)
            continue
        root = underlying_root(parts[1])
        if root not in newest or stamp > newest[root][0]:
            newest[root] = (stamp, path)
    return {root: path for root, (_stamp, path) in newest.items()}, other


def latest_snapshots(
    settings: Settings, on_or_before: date
) -> dict[str, list[dict[str, Any]]]:
    """Most recent snapshot sweep per underlying root, at or before a date."""
    import pyarrow.parquet as pq

    for dt in reversed(partition_dates(settings, "option_snapshots")):
        if dt > on_or_before:
            continue
        files, other = _latest_files_by_underlying(settings, "option_snapshots", dt)
        if not files and not other:
            continue
        out = {root: pq.read_table(path).to_pylist() for root, path in files.items()}
        for path in other:
            for rec in pq.read_table(path).to_pylist():
                out.setdefault(
                    underlying_root(rec.get("underlying_ticker") or ""), []
                ).append(rec)
        return out
    return {}


def latest_contracts(settings: Settings, on_or_before: date) -> list[dict[str, Any]]:
    """Current contract universe: newest ``contracts`` file per underlying.

    Deliberately not ``latest_clean_records("contracts", ...)`` -- that reads
    every parquet in the partition and so returns each contract once per
    ``contracts_sync`` run that day.
    """
    import pyarrow.parquet as pq

    for dt in reversed(partition_dates(settings, "contracts")):
        if dt > on_or_before:
            continue
        files, other = _latest_files_by_underlying(settings, "contracts", dt)
        if not files and not other:
            continue
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in list(files.values()) + other:
            for rec in pq.read_table(path).to_pylist():
                ticker = str(rec.get("ticker") or "")
                if ticker and ticker not in seen:
                    seen.add(ticker)
                    out.append(rec)
        return out
    return []


def forward_from_parity(
    snapshot_records: list[dict[str, Any]],
    rate_for_expiry: Callable[[date], float] | None = None,
    asof_date: date | None = None,
) -> list[dict[str, Any]]:
    """Per-expiry forward price recovered from put-call parity.

    For each expiration, take the strike minimising ``|C - P|`` among strikes
    quoting both a call and a put, and return ``F = K + C - P``. This is the
    standard forward-extraction step and needs only the option chain, which
    matters because no index endpoint on this tier will return the SPX level.

    Validated against live data on 2026-08-31: SPX expiries resolved to
    7684-7698 across the term structure, versus 7673.8 for the SPY close x10
    proxy (~0.2% low, consistent with dividend accrual).

    **For VIX this returns the VX future, not a spot VIX, and that is
    correct.** VIX options are options on the VIX future of their own expiry,
    so the per-expiry parity forward *is* that future -- which is the object a
    term-structure model wants. Do not "fix" this by discounting it toward a
    spot VIX; the two are different quantities and the vendor does not supply
    the index level on this tier anyway. Measured 2026-09-01, the curve came
    out in contango: 15.26 / 16.28 / 16.58 / 17.23 / 18.16 / 18.47.

    Returns records shaped for the ``forwards`` dataset, sorted by expiry.
    """
    by_expiry: dict[str, dict[float, dict[str, float]]] = {}
    asof = 0
    underlying = ""
    for rec in snapshot_records:
        exp = rec.get("details_expiration_date")
        strike = rec.get("details_strike_price")
        kind = rec.get("details_contract_type")
        close = rec.get("day_close")
        if not exp or strike is None or kind not in ("call", "put") or close is None:
            continue
        underlying = underlying or str(rec.get("underlying_ticker") or "")
        stamp = rec.get("day_last_updated_ns") or 0
        asof = max(asof, int(stamp or 0))
        by_expiry.setdefault(str(exp), {}).setdefault(float(strike), {})[kind] = float(close)

    out: list[dict[str, Any]] = []
    for exp, strikes in sorted(by_expiry.items()):
        pairs = [
            (k, v["call"], v["put"])
            for k, v in strikes.items()
            if "call" in v and "put" in v
        ]
        if not pairs:
            continue
        strike, call, put = min(pairs, key=lambda t: abs(t[1] - t[2]))
        discount = 1.0
        if rate_for_expiry is not None:
            try:
                expiry_date = date.fromisoformat(str(exp)[:10])
            except ValueError:
                expiry_date = None
            if expiry_date is not None:
                years = max((expiry_date - (asof_date or expiry_date)).days, 0) / 365.0
                if years > 0:
                    discount = math.exp(rate_for_expiry(expiry_date) * years)
        out.append({
            "underlying_ticker": underlying,
            "expiration_date": exp,
            "atm_strike": strike,
            "forward": strike + discount * (call - put),
            "call_price": call,
            "put_price": put,
            "pairs": len(pairs),
            "asof_ns": asof or None,
            "method": "parity" if rate_for_expiry is not None else "parity-r0",
        })
    return out


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def reference_price(
    root: str,
    snapshot_records: list[dict[str, Any]],
    spy_reference: float | None = None,
) -> tuple[float | None, str]:
    """Reference spot for one underlying root; returns ``(price, method)``.

    Resolution order:
      1. ``underlying_price`` from the snapshot (populated for SPY).
      2. Put-call parity on the chain -- the median near-dated forward. This
         is the only path that works for SPX, whose index level is 403 on
         every endpoint and null in the snapshot payload.
      3. SPY x 10 as a last-resort proxy for SPX (~0.2% low in practice).
    """
    prices = [
        float(r["underlying_price"])
        for r in snapshot_records
        if r.get("underlying_price")
    ]
    if prices:
        return prices[0], "spot"

    forwards = forward_from_parity(snapshot_records)
    near = _median([f["forward"] for f in forwards[:8] if f.get("forward")])
    if near is not None:
        return near, "parity"

    if root == "SPX" and spy_reference is not None:
        return spy_reference * 10.0, "proxy"
    return None, "none"


# ---------------------------------------------------------------------------
# Watchlist (shared by eod_dayaggs_rest and trades_watchlist)
# ---------------------------------------------------------------------------

def latest_spy_price(settings: Settings, on_or_before: date) -> float | None:
    """Latest SPY price: last close of underlying_minute_bars, else snapshot.

    Falls back to ``underlying_price`` from the most recent option_snapshots
    partition when no SPY minute bars have been landed yet.
    """
    bars = latest_clean_records(settings, "underlying_minute_bars", on_or_before)
    bars = [b for b in bars if b.get("ticker") == "SPY" and b.get("close") is not None]
    if bars:
        latest = max(bars, key=lambda b: b.get("start_ms") or 0)
        return float(latest["close"])
    snaps = latest_snapshots(settings, on_or_before).get("SPY", [])
    prices = [s["underlying_price"] for s in snaps if s.get("underlying_price")]
    if prices:
        return float(prices[0])
    return None


def _liquidity_index(
    snapshots: dict[str, list[dict[str, Any]]]
) -> dict[str, tuple[float, float]]:
    """``{ticker: (day_volume, open_interest)}`` from the latest sweeps."""
    out: dict[str, tuple[float, float]] = {}
    for records in snapshots.values():
        for rec in records:
            ticker = rec.get("ticker")
            if ticker:
                out[str(ticker)] = (
                    float(rec.get("day_volume") or 0),
                    float(rec.get("open_interest") or 0),
                )
    return out


def compute_watchlist(
    settings: Settings,
    run_date: date,
    logger: JsonlLogger | None = None,
    dte_min: int = DTE_MIN,
    dte_max: int = DTE_MAX,
    moneyness: float = MONEYNESS_PCT,
    require_liquidity: bool = True,
    min_open_interest: int = MIN_OPEN_INTEREST,
) -> list[dict[str, Any]]:
    """Watchlist contracts, banded per underlying and filtered for liquidity.

    Each underlying root gets its own reference price (see
    :func:`reference_price`) and therefore its own strike band, so SPX is
    banded around ~7,690 rather than around the SPY price. Contracts are kept
    when they expire in ``[dte_min, dte_max]`` days, sit within ``moneyness``
    of that underlying's reference, and -- when ``require_liquidity`` -- traded
    today or carry at least ``min_open_interest`` contracts of open interest.

    Raises RuntimeError when contracts or every reference price are
    unavailable (run contracts_sync / snapshot_sweep first).
    """
    contracts = latest_contracts(settings, run_date)
    if not contracts:
        raise RuntimeError(
            "no clean 'contracts' partition found at or before "
            f"{run_date}; run contracts_sync first"
        )

    snapshots = latest_snapshots(settings, run_date)
    liquidity = _liquidity_index(snapshots) if require_liquidity else {}
    spy_ref = latest_spy_price(settings, run_date)

    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in contracts:
        groups.setdefault(underlying_root(rec.get("underlying_ticker") or ""), []).append(rec)

    lo_d = run_date + timedelta(days=dte_min)
    hi_d = run_date + timedelta(days=dte_max)

    watchlist: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    for root, recs in sorted(groups.items()):
        ref, method = reference_price(root, snapshots.get(root, []), spy_ref)
        if ref is None:
            summary[root] = {"reference": None, "method": method, "selected": 0,
                             "universe": len(recs), "skipped": "no reference price"}
            continue
        lo_k, hi_k = ref * (1 - moneyness), ref * (1 + moneyness)
        in_band = 0
        selected = 0
        for rec in recs:
            exp_raw, strike = rec.get("expiration_date"), rec.get("strike_price")
            if not exp_raw or strike is None:
                continue
            try:
                exp = date.fromisoformat(str(exp_raw)[:10])
            except ValueError:
                continue
            if not (lo_d <= exp <= hi_d and lo_k <= float(strike) <= hi_k):
                continue
            in_band += 1
            if require_liquidity:
                volume, oi = liquidity.get(str(rec.get("ticker")), (0.0, 0.0))
                if volume <= 0 and oi < min_open_interest:
                    continue
            watchlist.append(rec)
            selected += 1
        summary[root] = {
            "reference": round(ref, 4),
            "method": method,
            "universe": len(recs),
            "strike_range": [round(lo_k, 2), round(hi_k, 2)],
            "in_band": in_band,
            "selected": selected,
        }

    if not watchlist:
        raise RuntimeError(
            "watchlist is empty for every underlying "
            f"({ {k: v.get('method') for k, v in summary.items()} }); "
            "run contracts_sync and snapshot_sweep first"
        )

    if logger is not None:
        logger.log(
            "watchlist",
            run_date=run_date.isoformat(),
            dte_range=[dte_min, dte_max],
            moneyness=moneyness,
            require_liquidity=require_liquidity,
            min_open_interest=min_open_interest,
            contracts=len(contracts),
            selected=len(watchlist),
            by_underlying=summary,
        )
    return watchlist
