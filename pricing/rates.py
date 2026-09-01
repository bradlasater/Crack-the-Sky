"""Discount rate from the landed Treasury curve, interpolated to maturity.

Every option in this repo was priced with a hardcoded ``r`` (0.04 by default,
or ``DRIFT_CHECK_R``). For a 5-45 DTE book the correct discount rate is the
short end of the par curve -- 3.84% (1M) to 3.90% (3M) as of 2026-08-28 -- and
a flat 4.00% pushes a small, systematic, one-directional error into every
inverted IV.

This reads what ``ingest.jobs.rates_sync`` lands and interpolates **linearly in
time to maturity** across the quoted tenors, holding the endpoints flat outside
them. Linear-in-T on par yields is deliberately unclever: it is transparent,
monotone between quotes, and cannot manufacture a curve shape the data does not
contain. Anything fancier (bootstrapping zeros, splining forwards) is a
modelling choice that belongs where the model lives, not in a rate lookup.

Rates are quoted in **percent** in the source and returned here as **decimals**
(3.84 -> 0.0384), because that is what every pricing entry point expects.

Falls back loudly: a missing partition or an unusable curve raises rather than
silently returning a default, since a wrong ``r`` is invisible in the output.
"""

from __future__ import annotations

import bisect
import os
from datetime import date
from pathlib import Path
from typing import Any

# Quoted tenors, in years. Only those the vendor actually populates are used;
# the schema carries 6M/3Y/7Y/20Y as nullable for completeness.
TENORS: tuple[tuple[str, float], ...] = (
    ("yield_1_month", 1.0 / 12.0),
    ("yield_3_month", 0.25),
    ("yield_6_month", 0.5),
    ("yield_1_year", 1.0),
    ("yield_2_year", 2.0),
    ("yield_3_year", 3.0),
    ("yield_5_year", 5.0),
    ("yield_7_year", 7.0),
    ("yield_10_year", 10.0),
    ("yield_20_year", 20.0),
    ("yield_30_year", 30.0),
)

DATASET = "treasury_yields"


class RateCurveError(RuntimeError):
    """No usable curve for the requested date."""


class RateCurve:
    """One day's par curve, interpolated in time to maturity."""

    __slots__ = ("date", "points")

    def __init__(self, curve_date: str, points: list[tuple[float, float]]) -> None:
        if not points:
            raise RateCurveError(f"curve for {curve_date} has no populated tenors")
        self.date = curve_date
        self.points = sorted(points)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> RateCurve:
        """Build from one ``treasury_yields`` record (percent -> decimal)."""
        pts = [
            (years, float(row[field]) / 100.0)
            for field, years in TENORS
            if row.get(field) is not None
        ]
        return cls(str(row.get("date") or ""), pts)

    def at(self, T: float) -> float:
        """Continuous-ish rate for maturity ``T`` years, flat outside the quotes."""
        if T <= 0:
            raise RateCurveError(f"maturity must be positive, got {T}")
        xs = [x for x, _ in self.points]
        if xs[0] >= T:
            return self.points[0][1]
        if xs[-1] <= T:
            return self.points[-1][1]
        i = bisect.bisect_left(xs, T)
        x0, y0 = self.points[i - 1]
        x1, y1 = self.points[i]
        return y0 + (y1 - y0) * (T - x0) / (x1 - x0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RateCurve({self.date}, {len(self.points)} tenors)"


def _data_root(data_root: str | os.PathLike[str] | None = None) -> Path:
    if data_root is not None:
        return Path(data_root)
    return Path(os.environ.get("DATA_ROOT", "/data/massive"))


def load_curve(
    on_or_before: date | str,
    data_root: str | os.PathLike[str] | None = None,
) -> RateCurve:
    """Most recent curve at or before ``on_or_before``.

    The par curve is published on business days, so a Monday option is priced
    off Friday's curve. Scans partitions newest-first and stops at the first
    match rather than reading the whole warehouse.
    """
    import pyarrow.parquet as pq

    want = on_or_before.isoformat() if isinstance(on_or_before, date) else str(on_or_before)
    root = _data_root(data_root) / "clean" / DATASET
    if not root.is_dir():
        raise RateCurveError(
            f"no {DATASET} data under {root}; run `python -m ingest.jobs.rates_sync`"
        )

    best: dict[str, Any] | None = None
    for part in sorted(root.glob("dt=*"), reverse=True):
        for path in sorted(part.glob("*.parquet")):
            for row in pq.read_table(path).to_pylist():
                d = str(row.get("date") or "")
                if d and d <= want and (best is None or d > str(best.get("date"))):
                    best = row
        if best is not None:
            # Partitions are landed newest-first; one hit is enough once the
            # whole partition has been scanned.
            break
    if best is None:
        raise RateCurveError(f"no {DATASET} row at or before {want}")
    return RateCurve.from_row(best)


def rate_for(
    on_or_before: date | str,
    T: float,
    data_root: str | os.PathLike[str] | None = None,
) -> float:
    """Discount rate (decimal) for maturity ``T`` years as of a date."""
    return load_curve(on_or_before, data_root).at(T)
