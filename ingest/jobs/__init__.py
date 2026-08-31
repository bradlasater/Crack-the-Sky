"""REST batch ingestion jobs (``python -m ingest.jobs.<name>``).

This package init carries the small helpers shared by several jobs:
clean-partition readers, the watchlist computation (7-45 DTE, +/-15%
moneyness around the latest SPY price) and CLI conveniences for
job-specific extra flags on top of the shared ``cli.run_job`` parser.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ingest.common import market_gate
from ingest.common.config import Settings
from ingest.common.logging_utils import JsonlLogger

# Watchlist parameters (SPEC): expiration 7-45 days out, strike within
# +/-15% of the latest SPY price.
DTE_MIN = 7
DTE_MAX = 45
MONEYNESS_PCT = 0.15

DAY_MS = 86_400_000


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
    """Read every parquet file of one clean partition into a list of dicts."""
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
    snaps = latest_clean_records(settings, "option_snapshots", on_or_before)
    prices = [s["underlying_price"] for s in snaps if s.get("underlying_price")]
    if prices:
        return float(prices[0])
    return None


def compute_watchlist(
    settings: Settings,
    run_date: date,
    logger: JsonlLogger | None = None,
    dte_min: int = DTE_MIN,
    dte_max: int = DTE_MAX,
    moneyness: float = MONEYNESS_PCT,
) -> list[dict[str, Any]]:
    """Watchlist contracts: 7-45 DTE and strike within +/-15% of SPY price.

    Contracts come from the latest ``contracts`` clean partition at or before
    ``run_date``. Raises RuntimeError when contracts or the SPY reference
    price are unavailable (run contracts_sync / underlying_bars first).
    """
    contracts = latest_clean_records(settings, "contracts", run_date)
    if not contracts:
        raise RuntimeError(
            "no clean 'contracts' partition found at or before "
            f"{run_date}; run contracts_sync first"
        )
    spy_price = latest_spy_price(settings, run_date)
    if spy_price is None:
        raise RuntimeError(
            "no SPY reference price found (underlying_minute_bars or "
            "option_snapshots); run underlying_bars or snapshot_sweep first"
        )
    lo_d, hi_d = run_date + timedelta(days=dte_min), run_date + timedelta(days=dte_max)
    lo_k, hi_k = spy_price * (1 - moneyness), spy_price * (1 + moneyness)
    watchlist = []
    for rec in contracts:
        exp_raw, strike = rec.get("expiration_date"), rec.get("strike_price")
        if not exp_raw or strike is None:
            continue
        try:
            exp = date.fromisoformat(str(exp_raw)[:10])
        except ValueError:
            continue
        if lo_d <= exp <= hi_d and lo_k <= float(strike) <= hi_k:
            watchlist.append(rec)
    if logger is not None:
        logger.log(
            "watchlist",
            run_date=run_date.isoformat(),
            spy_price=spy_price,
            strike_range=[round(lo_k, 2), round(hi_k, 2)],
            dte_range=[dte_min, dte_max],
            contracts=len(contracts),
            selected=len(watchlist),
        )
    return watchlist
