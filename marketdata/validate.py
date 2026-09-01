"""Fail-loud validation report. Absence is a failure.

Pattern after ``ingest.jobs.coverage_audit``: every check is named, absence
(empty allowlisted root, missing partition, required-nulls) is FAIL, and the
process exits nonzero.

Run: ``python -m marketdata.validate --dataset option_snapshots --date YYYY-MM-DD``
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marketdata.catalog import (
    ASOF_DATASETS,
    CatalogError,
    SchemaError,
    list_partitions,
    read_asof,
    read_partition,
)
from marketdata.opra import ALLOWED_ROOTS, ticker_root

PASS, FAIL = "PASS", "FAIL"

REQUIRED_NONNULL: dict[str, tuple[str, ...]] = {
    "contracts": (
        "ticker",
        "underlying_ticker",
        "contract_type",
        "expiration_date",
        "strike_price",
    ),
    "contracts_expired": (
        "ticker",
        "underlying_ticker",
        "contract_type",
        "expiration_date",
        "strike_price",
    ),
    "option_snapshots": (
        "ticker",
        "details_contract_type",
        "details_expiration_date",
        "details_strike_price",
    ),
    "option_minute_bars": ("ticker",),
    "option_day_bars": ("ticker",),
    "option_trades": ("ticker",),
    "forwards": ("underlying_ticker", "expiration_date", "forward"),
    "underlying_minute_bars": ("ticker",),
    "underlying_day_bars": ("ticker",),
}


@dataclass
class Check:
    """One assertion about a table or ticker set."""

    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


def validate_tickers(
    tickers: list[str],
    roots: tuple[str, ...] = ALLOWED_ROOTS,
) -> list[Check]:
    """Foreign roots, empty allowlisted root, malformed tickers."""
    checks: list[Check] = []
    allow = tuple(roots)
    counts: dict[str, int] = {}
    foreign: dict[str, int] = {}
    malformed = 0
    for ticker in tickers:
        root = ticker_root(ticker)
        if root is None:
            malformed += 1
            continue
        if root not in allow:
            foreign[root] = foreign.get(root, 0) + 1
        else:
            counts[root] = counts.get(root, 0) + 1

    if malformed:
        checks.append(
            Check(
                "ticker_parse",
                FAIL,
                f"{malformed} ticker(s) are not OPRA option symbols",
                {"malformed": malformed},
            )
        )
    else:
        checks.append(Check("ticker_parse", PASS, "all tickers parsed", {}))

    if foreign:
        checks.append(
            Check(
                "ticker_purity",
                FAIL,
                "foreign roots: " + ", ".join(f"{k}={v}" for k, v in sorted(foreign.items())),
                dict(foreign),
            )
        )
    else:
        checks.append(
            Check(
                "ticker_purity",
                PASS,
                f"only {'/'.join(allow)} present",
                {},
            )
        )

    if not counts and not foreign and not malformed:
        checks.append(Check("rows", FAIL, "no tickers to validate", {}))

    for root in allow:
        n = counts.get(root, 0)
        if n == 0:
            checks.append(
                Check(
                    f"root[{root}]",
                    FAIL,
                    "empty allowlisted root (absence is a failure)",
                    {"rows": 0},
                )
            )
        else:
            checks.append(Check(f"root[{root}]", PASS, f"{n} tickers", {"rows": n}))
    return checks


def _null_count(table: Any, column: str) -> int:
    col = table[column]
    if col.null_count is None:
        return 0
    return int(col.null_count)


def validate_table(
    table: Any,
    dataset: str,
    roots: tuple[str, ...] = ALLOWED_ROOTS,
) -> list[Check]:
    """Schema already validated by the catalog. Check nulls, roots, emptiness."""
    checks: list[Check] = []
    if table.num_rows == 0:
        checks.append(Check("rows", FAIL, "empty table (absence is a failure)", {"rows": 0}))
        return checks
    checks.append(Check("rows", PASS, f"{table.num_rows} rows", {"rows": table.num_rows}))

    for col in REQUIRED_NONNULL.get(dataset, ()):
        if col not in table.column_names:
            checks.append(Check(f"required[{col}]", FAIL, "column missing", {}))
            continue
        nnull = _null_count(table, col)
        if nnull:
            checks.append(
                Check(
                    f"required[{col}]",
                    FAIL,
                    f"{nnull} nulls in required column",
                    {"nulls": nnull},
                )
            )
        else:
            checks.append(Check(f"required[{col}]", PASS, "no nulls", {"nulls": 0}))

    if "ticker" in table.column_names:
        tickers = [str(t) if t is not None else "" for t in table["ticker"].to_pylist()]
        checks.extend(validate_tickers(tickers, roots=roots))
    return checks


def _render(checks: list[Check]) -> str:
    width = max((len(c.name) for c in checks), default=10)
    lines = [f"{c.status:<5} {c.name:<{width}}  {c.detail}" for c in checks]
    nfail = sum(1 for c in checks if c.status == FAIL)
    lines.append(f"FAIL={nfail}  PASS={len(checks) - nfail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry; returns 0 on all PASS, 1 on any FAIL."""
    parser = argparse.ArgumentParser(prog="python -m marketdata.validate")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--date", required=True, help="partition date YYYY-MM-DD")
    parser.add_argument(
        "--roots",
        default=",".join(ALLOWED_ROOTS),
        help="comma-separated OPRA roots to require (default SPY,SPX,SPXW)",
    )
    parser.add_argument("--asof-ns", type=int, default=None)
    parser.add_argument(
        "--data-root",
        default=os.environ.get("DATA_ROOT", "/data/massive"),
    )
    args = parser.parse_args(argv)
    roots = tuple(r.strip() for r in args.roots.split(",") if r.strip())
    dt = date.fromisoformat(args.date)

    try:
        if args.dataset in ASOF_DATASETS:
            table = read_asof(args.dataset, dt, asof_ns=args.asof_ns, data_root=args.data_root)
        else:
            table = read_partition(args.dataset, dt, data_root=args.data_root)
    except (CatalogError, SchemaError) as exc:
        print(f"FAIL  catalog  {exc}", file=sys.stderr)
        return 1

    # SPY-only query: mixed underlyings (SPX in a SPY allowlist) are foreign.
    checks = validate_table(table, args.dataset, roots=roots)
    print(_render(checks), file=sys.stderr)
    if any(c.status == FAIL for c in checks):
        return 1
    # list_partitions is the other absence check: a date not in the catalog.
    if dt not in list_partitions(args.dataset, data_root=args.data_root):
        print("FAIL  partition  date missing from catalog", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
