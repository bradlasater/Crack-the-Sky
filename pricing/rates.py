"""Discount curve for pricing -- re-exported from :mod:`ingest.common.rates`.

The implementation lives on the ingest side because it reads landed parquet
and must stay importable by the ingest jobs, which deliberately do not depend
on numpy/scipy. This module exists so pricing code can say
``from pricing.rates import rate_for`` without reaching across the boundary.
"""

from __future__ import annotations

from ingest.common.rates import (
    DATASET,
    TENORS,
    RateCurve,
    RateCurveError,
    load_curve,
    rate_for,
)

__all__ = [
    "DATASET",
    "TENORS",
    "RateCurve",
    "RateCurveError",
    "load_curve",
    "rate_for",
]
