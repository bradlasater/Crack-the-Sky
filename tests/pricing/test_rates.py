"""Treasury curve lookup and interpolation.

Before this existed every option was priced with a hardcoded r (0.04). The real
short end was 3.84%, which is a small but one-directional error in every
inverted IV: 2.7bp at 7 DTE rising to 6.9bp at 45 DTE.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ingest.common import landing
from pricing.rates import TENORS, RateCurve, RateCurveError, load_curve, rate_for

# The real 2026-08-28 curve, in percent as the vendor quotes it.
CURVE_2026_08_28 = {
    "date": "2026-08-28",
    "yield_1_month": 3.84, "yield_3_month": 3.90, "yield_1_year": 4.15,
    "yield_2_year": 4.34, "yield_5_year": 4.48, "yield_10_year": 4.73,
    "yield_30_year": 5.22,
}


def _land(tmp_path: Path, *rows: dict) -> None:
    landing.write_clean("treasury_yields", date(2026, 8, 31), list(rows),
                        job="rates_sync", data_root=tmp_path)


# ---------------------------------------------------------------------------
# Interpolation
# ---------------------------------------------------------------------------

def test_percent_is_converted_to_decimal() -> None:
    c = RateCurve.from_row(CURVE_2026_08_28)
    assert c.at(1.0 / 12) == pytest.approx(0.0384)
    assert c.at(30.0) == pytest.approx(0.0522)


def test_short_end_is_flat_below_the_first_tenor() -> None:
    """A 7 DTE option sits inside the 1M point; do not extrapolate."""
    c = RateCurve.from_row(CURVE_2026_08_28)
    assert c.at(7 / 365) == pytest.approx(0.0384)
    assert c.at(1 / 365) == pytest.approx(0.0384)


def test_long_end_is_flat_above_the_last_tenor() -> None:
    c = RateCurve.from_row(CURVE_2026_08_28)
    assert c.at(50.0) == pytest.approx(0.0522)


def test_interpolates_between_quotes_and_stays_monotone() -> None:
    c = RateCurve.from_row(CURVE_2026_08_28)
    r45 = c.at(45 / 365)                    # between 1M and 3M
    assert 0.0384 < r45 < 0.0390
    r6m = c.at(0.5)                         # between 3M and 1Y
    assert 0.0390 < r6m < 0.0415
    # monotone across the whole quoted range for this (upward) curve
    xs = [i / 100 for i in range(1, 3000)]
    ys = [c.at(x) for x in xs]
    assert all(b >= a - 1e-12 for a, b in zip(ys, ys[1:], strict=False))


def test_missing_tenors_are_skipped_not_zeroed() -> None:
    """The vendor populates 7 of 11 tenors; nulls must not become 0%."""
    c = RateCurve.from_row(CURVE_2026_08_28)
    assert len(c.points) == 7
    assert all(y > 0 for _, y in c.points)
    assert len(TENORS) == 11


def test_non_positive_maturity_raises() -> None:
    c = RateCurve.from_row(CURVE_2026_08_28)
    with pytest.raises(RateCurveError, match="positive"):
        c.at(0.0)


def test_curve_with_no_populated_tenors_raises() -> None:
    with pytest.raises(RateCurveError, match="no populated tenors"):
        RateCurve.from_row({"date": "2026-08-28"})


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def test_loads_the_curve_for_an_exact_date(tmp_path: Path) -> None:
    _land(tmp_path, CURVE_2026_08_28)
    assert load_curve("2026-08-28", tmp_path).date == "2026-08-28"
    assert rate_for("2026-08-28", 30 / 365, tmp_path) == pytest.approx(0.0384)


def test_weekend_falls_back_to_the_previous_business_day(tmp_path: Path) -> None:
    """The par curve is published on business days; Monday uses Friday."""
    _land(tmp_path, CURVE_2026_08_28)
    assert load_curve(date(2026, 8, 30), tmp_path).date == "2026-08-28"


def test_never_looks_into_the_future(tmp_path: Path) -> None:
    _land(tmp_path, CURVE_2026_08_28,
          {**CURVE_2026_08_28, "date": "2026-09-04", "yield_1_month": 9.99})
    c = load_curve("2026-08-31", tmp_path)
    assert c.date == "2026-08-28"
    assert c.at(1 / 12) == pytest.approx(0.0384)


def test_missing_data_fails_loudly(tmp_path: Path) -> None:
    """A silently defaulted rate is invisible and wrong in every output."""
    with pytest.raises(RateCurveError, match="rates_sync"):
        load_curve("2026-08-28", tmp_path)


def test_date_before_all_data_fails_loudly(tmp_path: Path) -> None:
    _land(tmp_path, CURVE_2026_08_28)
    with pytest.raises(RateCurveError, match="at or before"):
        load_curve("1999-01-01", tmp_path)


# ---------------------------------------------------------------------------
# dt= is the ingestion run date, not the curve date
# ---------------------------------------------------------------------------

def test_older_partition_can_hold_the_newer_curve(tmp_path: Path) -> None:
    """A resumed --full walk writes old history into a *new* partition.

    Stopping at the first partition containing any match would then return a
    decades-stale curve for a current quote.
    """
    # Older run date, recent curve.
    landing.write_clean("treasury_yields", date(2026, 9, 1), [CURVE_2026_08_28],
                        job="rates_sync", data_root=tmp_path)
    # Newer run date, ancient history (what a resumed backfill produces).
    landing.write_clean(
        "treasury_yields", date(2026, 9, 2),
        [{**CURVE_2026_08_28, "date": "1962-01-02", "yield_1_month": 2.5}],
        job="rates_sync", data_root=tmp_path,
    )
    curve = load_curve("2026-08-31", tmp_path)
    assert curve.date == "2026-08-28", "must not return the 1962 row"
    assert curve.at(1 / 12) == pytest.approx(0.0384)


def test_scans_every_partition_for_the_global_best(tmp_path: Path) -> None:
    for run_dt, curve_dt in ((date(2026, 9, 1), "2026-08-20"),
                             (date(2026, 9, 2), "2026-08-28"),
                             (date(2026, 9, 3), "1999-01-04")):
        landing.write_clean("treasury_yields", run_dt,
                            [{**CURVE_2026_08_28, "date": curve_dt}],
                            job="rates_sync", data_root=tmp_path)
    assert load_curve("2026-09-30", tmp_path).date == "2026-08-28"
    assert load_curve("2026-08-25", tmp_path).date == "2026-08-20"
    assert load_curve("2000-01-01", tmp_path).date == "1999-01-04"
