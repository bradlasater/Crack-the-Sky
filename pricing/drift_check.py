"""Daily canary: own IV/Greeks identities on the as-of chain.

The live test is **our math on new quotes**, not vendor-as-gospel. We always
invert own IV and compute own Greeks from the as-of snapshot
(:mod:`pricing.from_market`). Pass/fail is identity / self-consistency on
ATM names (``|K/F − 1| ≤ atm_pct``, default 5%):

* stored engine price reprices the input: ``market_price`` vs ``own_price``
  (CRR for American rows — an independent check; BSM for European rows)
* European put–call pairs: ``Γ_call ≈ Γ_put``, ``vega_call ≈ vega_put``,
  and put–call parity on day-close quotes vs ``S e^{-qT} − K e^{-rT}``
  (PCP is skipped when either leg carries a last-trade price, which may
  come from a different moment in the session than its pair)

Far-OTM ticks are not the trigger. The job FAILs when the median reprice
error exceeds its band, or when more than ``fail_frac`` of ATM names fail
identities — the same median / fraction philosophy as before.

Vendor diffs are **diagnostic**. When vendor IV/greeks are present they are
unit-aligned (theta ``× 365``, vega ``× 100``) and written into the report.
Vendor drift FAILs only if there are at least ``min_compare`` ATM names with
vendor IV. Null vendor IV is **not** a FAIL: the report logs
``vendor_compare_skipped`` and the job still PASSes when identities hold.

Still fail-loud for: no snapshots, schema errors, foreign roots, empty
allowlisted root, too few ATM names to test identities.

Cron bounds
-----------
SPY CRR is limited with ``--spy-atm-pct`` (default 5%) and ``--max-rows``
(default 400, CRR rows only — not a global chain cap). Combined with
``--atm-pct``, only the ATM slice is inverted.
``--r`` defaults to ``DRIFT_CHECK_R``; with neither set, the rate comes from
the landed Treasury curve (:mod:`ingest.common.rates`), interpolated to each
contract's own maturity. ``DEFAULT_R`` remains only as the fallback for a box
where ``rates_sync`` has never run.

Run: ``python -m pricing.drift_check [--date YYYY-MM-DD]``
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa

from ingest.common import config as _config
from ingest.common.cli import healthcheck_slug, ping
from ingest.common.landing import meta_path
from ingest.common.logging_utils import get_run_logger
from ingest.common.market_gate import require_trading_day, today_et
from marketdata.catalog import CatalogError, SchemaError
from marketdata.opra import ALLOWED_ROOTS
from marketdata.validate import narrow_roots
from pricing.bsm import greeks as bsm_greeks
from pricing.conventions import CALENDAR_DAYS_PER_YEAR
from pricing.from_market import (
    CHAIN_CRR_STEPS,
    ChainCounts,
    ChainError,
    greeks_asof,
)

JOB = "drift_check"
REPORT_NAME = "drift_check.json"
ET = ZoneInfo("America/New_York")

# Last snapshot at or before this clock time (EOD sweep is 16:35 ET).
DEFAULT_CUTOFF_ET = "16:40"
DEFAULT_R = 0.04
DEFAULT_ATM_PCT = 0.05
DEFAULT_SPY_ATM_PCT = 0.05
DEFAULT_MAX_ROWS = 400
DEFAULT_FAIL_FRAC = 0.25
DEFAULT_MIN_COMPARE = 20

# Vendor snapshot → our catalog units. Documented in the module docstring.
VENDOR_THETA_TO_YEAR = float(CALENDAR_DAYS_PER_YEAR)
VENDOR_VEGA_TO_PER_1 = 100.0

CORE_GREEKS: tuple[str, ...] = ("iv", "delta", "vega", "gamma", "theta")
PASS, FAIL = "PASS", "FAIL"


class DriftError(RuntimeError):
    """Raised when the canary trips, so the CLI exits 1."""


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Abs + rel bands and the ATM-set fail rule (identities, then vendor)."""

    iv_abs: float = 0.04
    iv_rel: float = 0.25
    delta_abs: float = 0.08
    delta_rel: float = 0.30
    gamma_abs: float = 0.005
    gamma_rel: float = 0.75
    vega_abs: float = 25.0
    vega_rel: float = 0.50
    theta_abs: float = 150.0
    theta_rel: float = 0.60
    iv_median_abs: float = 0.04
    reprice_abs: float = 0.05
    reprice_rel: float = 0.01
    reprice_median_abs: float = 0.05
    gamma_pair_abs: float = 0.002
    gamma_pair_rel: float = 0.35
    vega_pair_abs: float = 10.0
    vega_pair_rel: float = 0.35
    pcp_abs: float = 1.0
    pcp_rel: float = 0.05
    fail_frac: float = DEFAULT_FAIL_FRAC
    min_compare: int = DEFAULT_MIN_COMPARE
    atm_pct: float = DEFAULT_ATM_PCT


DEFAULT_THRESHOLDS = Thresholds()


@dataclass
class DriftReport:
    """One day's canary result (also the ``_meta/drift_check.json`` payload)."""

    date: str
    asof_ns: int | None
    cutoff_et: str
    r: float | None
    status: str
    failures: list[str] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)
    median_abs_iv: float | None = None
    median_abs_reprice: float | None = None
    frac_beyond: float | None = None
    frac_identity_beyond: float | None = None
    beyond_by_greek: dict[str, int] = field(default_factory=dict)
    beyond_by_identity: dict[str, int] = field(default_factory=dict)
    vendor_compare_skipped: bool = False
    thresholds: dict[str, Any] = field(default_factory=dict)
    units: dict[str, Any] = field(default_factory=dict)
    spy_atm_pct: float | None = None
    max_rows: int | None = None
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _opt(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def align_vendor(row: Mapping[str, Any]) -> dict[str, float | None]:
    """Vendor snapshot greeks in **our** catalog units.

    theta: per calendar day → per year (``* 365``).
    vega:  per 1% vol → per 1.00 vol (``* 100``).
    IV / delta / gamma are already in the same units as ours.
    """
    theta = _opt(row.get("vendor_theta"))
    vega = _opt(row.get("vendor_vega"))
    return {
        "iv": _opt(row.get("vendor_iv")),
        "delta": _opt(row.get("vendor_delta")),
        "gamma": _opt(row.get("vendor_gamma")),
        "theta": None if theta is None else theta * VENDOR_THETA_TO_YEAR,
        "vega": None if vega is None else vega * VENDOR_VEGA_TO_PER_1,
    }


def _own(row: Mapping[str, Any], greek: str) -> float | None:
    key = "own_iv" if greek == "iv" else f"own_{greek}"
    return _opt(row.get(key))


def beyond_band(own: float, vendor: float, abs_thr: float, rel_thr: float) -> bool:
    """True when the aligned residual exceeds the abs/rel envelope."""
    err = abs(own - vendor)
    scale = max(abs(own), abs(vendor), 1e-12)
    return err > max(abs_thr, rel_thr * scale)


def is_atm(row: Mapping[str, Any], atm_pct: float) -> bool:
    fwd = _opt(row.get("F"))
    strike = _opt(row.get("strike"))
    if fwd is None or strike is None or fwd <= 0:
        return False
    return abs(strike / fwd - 1.0) <= atm_pct


def _thr_for(thresholds: Thresholds, greek: str) -> tuple[float, float]:
    return {
        "iv": (thresholds.iv_abs, thresholds.iv_rel),
        "delta": (thresholds.delta_abs, thresholds.delta_rel),
        "gamma": (thresholds.gamma_abs, thresholds.gamma_rel),
        "vega": (thresholds.vega_abs, thresholds.vega_rel),
        "theta": (thresholds.theta_abs, thresholds.theta_rel),
    }[greek]


def _pair_key(row: Mapping[str, Any]) -> tuple[str, str, float] | None:
    root = row.get("root")
    expiry = row.get("expiry")
    strike = _opt(row.get("strike"))
    if root is None or expiry is None or strike is None:
        return None
    return (str(root), str(expiry), strike)


def _cp_side(row: Mapping[str, Any]) -> str | None:
    raw = row.get("call_put")
    if raw is None:
        return None
    token = str(raw).strip().lower()
    if token in ("call", "c"):
        return "call"
    if token in ("put", "p"):
        return "put"
    return None


def _is_european_row(row: Mapping[str, Any]) -> bool:
    """Pair identities apply to European names; American early exercise does not."""
    style = str(row.get("exercise_style") or "").strip().lower()
    engine = str(row.get("greeks_engine") or "").strip().lower()
    return not (style == "american" or engine.startswith("american"))


def atm_pairs(
    rows: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair ATM calls and puts that share root, expiry, and strike."""
    buckets: dict[tuple[str, str, float], dict[str, dict[str, Any]]] = {}
    for rec in rows:
        key = _pair_key(rec)
        side = _cp_side(rec)
        if key is None or side is None:
            continue
        buckets.setdefault(key, {})[side] = rec
    return [
        (sides["call"], sides["put"])
        for sides in buckets.values()
        if "call" in sides and "put" in sides
    ]


def _bsm_gamma_vega(
    row: Mapping[str, Any], sigma: float
) -> tuple[float, float] | None:
    """BSM gamma and vega at *sigma* for *row*'s market inputs.

    Returns ``None`` when any required input is missing or non-positive.
    Gamma and vega are independent of the call/put flag, so the same value
    is returned regardless of which side the row represents.
    """
    spot = _opt(row.get("S"))
    strike = _opt(row.get("strike"))
    tte = _opt(row.get("T"))
    rate = _opt(row.get("r"))
    cp = _cp_side(row)
    if None in (spot, strike, tte, rate, cp):
        return None
    if spot <= 0 or strike <= 0 or tte <= 0 or sigma <= 0:
        return None
    q = _opt(row.get("q"))
    if q is None:
        q = 0.0
    try:
        g = bsm_greeks(spot, strike, tte, rate, sigma, cp, q=q)
        return float(g.gamma), float(g.vega)
    except (ValueError, KeyError):
        return None


def _pcp_rhs(row: Mapping[str, Any]) -> float | None:
    """``S e^{-qT} - K e^{-rT}`` (put-call parity right-hand side)."""
    spot = _opt(row.get("S"))
    strike = _opt(row.get("strike"))
    tte = _opt(row.get("T"))
    rate = _opt(row.get("r"))
    if None in (spot, strike, tte, rate):
        return None
    q = _opt(row.get("q"))
    if q is None:
        q = 0.0
    return spot * math.exp(-q * tte) - strike * math.exp(-rate * tte)


def evaluate_drift(
    table: pa.Table,
    *,
    counts: ChainCounts | None = None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    dt: date,
    asof_ns: int | None,
    cutoff_et: str = DEFAULT_CUTOFF_ET,
    r: float | None,
    spy_atm_pct: float | None = None,
    max_rows: int | None = None,
) -> DriftReport:
    """ATM identity checks (pass/fail) plus optional vendor diagnostics."""
    rows = table.to_pylist()
    atm = [rec for rec in rows if is_atm(rec, thresholds.atm_pct)]
    vendor_atm = [rec for rec in atm if _opt(rec.get("vendor_iv")) is not None]
    n_no_vendor = len(atm) - len(vendor_atm)

    failures: list[str] = []
    beyond_by_identity = {"reprice": 0, "gamma_pair": 0, "vega_pair": 0, "pcp": 0}
    beyond_by_greek = dict.fromkeys(CORE_GREEKS, 0)

    pairs = atm_pairs(atm)
    n_euro_pairs = 0
    pair_failed: set[int] = set()
    for call, put in pairs:
        if not (_is_european_row(call) and _is_european_row(put)):
            continue
        n_euro_pairs += 1
        hit = False
        iv_c, iv_p = _own(call, "iv"), _own(put, "iv")
        if iv_c is not None and iv_p is not None:
            sigma_shared = (iv_c + iv_p) / 2.0
            gv_c = _bsm_gamma_vega(call, sigma_shared)
            gv_p = _bsm_gamma_vega(put, sigma_shared)
        else:
            gv_c = gv_p = None
        gamma_c = gv_c[0] if gv_c is not None else None
        gamma_p = gv_p[0] if gv_p is not None else None
        if (
            gamma_c is not None
            and gamma_p is not None
            and beyond_band(
                gamma_c, gamma_p, thresholds.gamma_pair_abs, thresholds.gamma_pair_rel
            )
        ):
            beyond_by_identity["gamma_pair"] += 1
            hit = True
        vega_c = gv_c[1] if gv_c is not None else None
        vega_p = gv_p[1] if gv_p is not None else None
        if (
            vega_c is not None
            and vega_p is not None
            and beyond_band(
                vega_c, vega_p, thresholds.vega_pair_abs, thresholds.vega_pair_rel
            )
        ):
            beyond_by_identity["vega_pair"] += 1
            hit = True
        # PCP requires contemporaneous prices; skip when either leg uses a
        # last-trade price that may be from a different moment in the session.
        call_src = call.get("price_source")
        put_src = put.get("price_source")
        if call_src == "close" and put_src == "close":
            call_px = _opt(call.get("market_price"))
            put_px = _opt(put.get("market_price"))
            rhs = _pcp_rhs(call)
            if (
                call_px is not None
                and put_px is not None
                and rhs is not None
                and beyond_band(
                    call_px - put_px, rhs, thresholds.pcp_abs, thresholds.pcp_rel
                )
            ):
                beyond_by_identity["pcp"] += 1
                hit = True
        if hit:
            pair_failed.add(id(call))
            pair_failed.add(id(put))

    reprice_abs: list[float] = []
    n_identity_beyond = 0
    for rec in atm:
        hit = id(rec) in pair_failed
        # Use the stored engine output (CRR for American rows, BSM for European)
        # rather than re-inverting via BSM, so American/CRR rows are validated
        # through their actual code path.
        model = _opt(rec.get("own_price"))
        mkt = _opt(rec.get("market_price"))
        if model is None or mkt is None:
            beyond_by_identity["reprice"] += 1
            hit = True
        else:
            reprice_abs.append(abs(model - mkt))
            if beyond_band(model, mkt, thresholds.reprice_abs, thresholds.reprice_rel):
                beyond_by_identity["reprice"] += 1
                hit = True
        if hit:
            n_identity_beyond += 1

    median_reprice = _median(reprice_abs) if reprice_abs else None
    frac_identity = (n_identity_beyond / len(atm)) if atm else None

    if len(atm) < thresholds.min_compare:
        failures.append(
            f"only {len(atm)} ATM names to test identities "
            f"(need ≥ {thresholds.min_compare})"
        )
    if (
        median_reprice is not None
        and median_reprice > thresholds.reprice_median_abs
    ):
        failures.append(
            f"median |price − own_price|={median_reprice:.4f} exceeds "
            f"{thresholds.reprice_median_abs}"
        )
    if frac_identity is not None and frac_identity > thresholds.fail_frac:
        failures.append(
            f"{n_identity_beyond}/{len(atm)} ATM names fail identities "
            f"({frac_identity:.0%} > {thresholds.fail_frac:.0%})"
        )

    iv_abs: list[float] = []
    n_vendor_beyond = 0
    for rec in vendor_atm:
        aligned = align_vendor(rec)
        hit = False
        own_iv = _own(rec, "iv")
        vend_iv = aligned["iv"]
        if own_iv is not None and vend_iv is not None:
            iv_abs.append(abs(own_iv - vend_iv))
        for greek in CORE_GREEKS:
            own = _own(rec, greek)
            vend = aligned[greek]
            if own is None or vend is None:
                continue
            abs_thr, rel_thr = _thr_for(thresholds, greek)
            if beyond_band(own, vend, abs_thr, rel_thr):
                beyond_by_greek[greek] += 1
                hit = True
        if hit:
            n_vendor_beyond += 1

    median_iv = _median(iv_abs) if iv_abs else None
    frac_vendor = (n_vendor_beyond / len(vendor_atm)) if vendor_atm else None
    vendor_compare_skipped = len(vendor_atm) < thresholds.min_compare
    if not vendor_compare_skipped:
        if median_iv is not None and median_iv > thresholds.iv_median_abs:
            failures.append(
                f"median |ΔIV|={median_iv:.4f} exceeds {thresholds.iv_median_abs}"
            )
        if frac_vendor is not None and frac_vendor > thresholds.fail_frac:
            failures.append(
                f"{n_vendor_beyond}/{len(vendor_atm)} vendor ATM names beyond band "
                f"({frac_vendor:.0%} > {thresholds.fail_frac:.0%})"
            )

    chain_counts = {
        "quotes": counts.n_quotes if counts else None,
        "priced": counts.n_priced if counts else table.num_rows,
        "expired": counts.n_expired if counts else None,
        "no_price": counts.n_no_price if counts else None,
        "uninvertible": counts.n_uninvertible if counts else None,
        "otm": counts.n_otm if counts else None,
        "atm_compared": len(atm),
        "atm_pairs": n_euro_pairs,
        "no_vendor_iv": n_no_vendor,
        "vendor_compared": len(vendor_atm),
        "identity_beyond": n_identity_beyond,
        "beyond_band": n_vendor_beyond,
    }
    status = FAIL if failures else PASS
    return DriftReport(
        date=dt.isoformat(),
        asof_ns=asof_ns,
        cutoff_et=cutoff_et,
        r=r,
        status=status,
        failures=failures,
        counts=chain_counts,
        median_abs_iv=median_iv,
        median_abs_reprice=median_reprice,
        frac_beyond=frac_vendor,
        frac_identity_beyond=frac_identity,
        beyond_by_greek=beyond_by_greek,
        beyond_by_identity=beyond_by_identity,
        vendor_compare_skipped=vendor_compare_skipped,
        thresholds=asdict(thresholds),
        units={
            "own": {"vega": "per_1.00", "theta": "per_year"},
            "vendor": {"vega": "per_1pct", "theta": "per_calendar_day"},
            "compare": {
                "vega": f"vendor_vega * {VENDOR_VEGA_TO_PER_1:g}",
                "theta": f"vendor_theta * {VENDOR_THETA_TO_YEAR:g}",
                "iv": "decimal (no conversion)",
                "delta": "spot (no conversion)",
                "gamma": "spot (no conversion)",
            },
            "identities": {
                "reprice": "market_price vs own_price (CRR for American, BSM for European)",
                "pairs": "european Γ_call≈Γ_put, vega_call≈vega_put, PCP",
            },
        },
        spy_atm_pct=spy_atm_pct,
        max_rows=max_rows,
        generated_at=datetime.now(ET).isoformat(timespec="seconds"),
    )


def cutoff_asof_ns(dt: date, cutoff_et: str = DEFAULT_CUTOFF_ET) -> int:
    """Epoch-ns of ``cutoff_et`` (HH:MM) on ``dt`` in America/New_York."""
    parts = cutoff_et.split(":")
    if len(parts) != 2:
        raise DriftError(f"cutoff-et must be HH:MM, got {cutoff_et!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise DriftError(f"cutoff-et must be HH:MM, got {cutoff_et!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise DriftError(f"cutoff-et out of range: {cutoff_et!r}")
    instant = datetime(dt.year, dt.month, dt.day, hour, minute, tzinfo=ET)
    return int(instant.timestamp() * 1e9)


def _render(report: DriftReport) -> str:
    vendor_note = (
        "vendor_compare_skipped"
        if report.vendor_compare_skipped
        else f"median_|ΔIV|={report.median_abs_iv}"
    )
    lines = [
        f"drift_check -- {report.date}  asof_ns={report.asof_ns}  "
        f"cutoff_et={report.cutoff_et} ET",
        "-" * 72,
        f"{report.status:<5} identities median_|price−own_price|={report.median_abs_reprice}  "
        f"frac_identity={report.frac_identity_beyond}  "
        f"atm={report.counts.get('atm_compared')}  "
        f"pairs={report.counts.get('atm_pairs')}",
        f"      {vendor_note}  frac_vendor={report.frac_beyond}  "
        f"vendor_atm={report.counts.get('vendor_compared')}",
        f"      priced={report.counts.get('priced')}  "
        f"expired={report.counts.get('expired')}  "
        f"no_price={report.counts.get('no_price')}  "
        f"uninvertible={report.counts.get('uninvertible')}  "
        f"otm={report.counts.get('otm')}",
        f"      beyond_by_identity={report.beyond_by_identity}",
        f"      beyond_by_greek={report.beyond_by_greek}",
        f"      units: vendor theta * {VENDOR_THETA_TO_YEAR:g} (day→year), "
        f"vendor vega * {VENDOR_VEGA_TO_PER_1:g} (1%→1.00)",
    ]
    for msg in report.failures:
        lines.append(f"FAIL  {msg}")
    lines.append("-" * 72)
    return "\n".join(lines)


def _dotenv() -> dict[str, str]:
    # Resolved through the module rather than bound at import time. A direct
    # ``from ... import _parse_env_file`` alias cannot be patched by the test
    # harness -- the name is captured before any fixture runs -- so the
    # credential scrubbing in tests/conftest.py silently did not cover this
    # job, and it is the one that pings on every run.
    return _config._parse_env_file(Path(".env"))


def _get(name: str, default: str | None = None, file_vals: Mapping[str, str] | None = None) -> str | None:
    if name in os.environ:
        return os.environ[name]
    if file_vals and name in file_vals:
        return file_vals[name]
    return default


def default_r(file_vals: Mapping[str, str] | None = None) -> float | None:
    """Explicit DRIFT_CHECK_R, else None so the Treasury curve is used.

    Returning None rather than DEFAULT_R is the point: the curve is the whole
    reason the rates warehouse exists, and a constant here would keep the
    scheduled canary pricing at 4% no matter what the short end is doing.
    DEFAULT_R survives only as the documented fallback when no curve has been
    landed yet.
    """
    raw = _get("DRIFT_CHECK_R", None, file_vals)
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise DriftError(f"DRIFT_CHECK_R is not a float: {raw!r}") from exc
    if not math.isfinite(value):
        raise DriftError(f"non-finite DRIFT_CHECK_R: {raw!r}")
    return value


def _hc_target(file_vals: Mapping[str, str] | None = None) -> tuple[str | None, bool]:
    key = _get("HEALTHCHECKS_PING_KEY", None, file_vals)
    base = (_get("HEALTHCHECKS_BASE", "https://hc-ping.com", file_vals) or "https://hc-ping.com").rstrip(
        "/"
    )
    if key:
        return f"{base}/{key}/{healthcheck_slug(JOB)}", True
    # No shared-URL fallback: HEALTHCHECKS_PING_URL is rejected repo-wide
    # (see Settings.load), so monitoring is simply off without a ping key.
    return None, False


def _webhook_body(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Slack incoming webhooks need ``text``; other endpoints get the report JSON."""
    if "hooks.slack.com" in url:
        status = payload.get("status", FAIL)
        date_s = payload.get("date", "")
        lines = [f"{status} drift_check {date_s}".strip()]
        for msg in payload.get("failures") or []:
            lines.append(str(msg))
        return {"text": "\n".join(lines)}
    return payload


def _post_webhook(url: str, payload: dict[str, Any]) -> None:
    """Best-effort POST of the FAIL report. Never raises."""
    try:
        data = json.dumps(_webhook_body(url, payload), default=str).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5):
            pass
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"warning: drift webhook failed: {exc}", file=sys.stderr)


def run_drift(
    dt: date,
    *,
    r: float | None,
    data_root: str | os.PathLike[str] | None,
    asof_ns: int | None = None,
    cutoff_et: str = DEFAULT_CUTOFF_ET,
    roots: tuple[str, ...] = ALLOWED_ROOTS,
    crr_steps: int = CHAIN_CRR_STEPS,
    spy_atm_pct: float | None = DEFAULT_SPY_ATM_PCT,
    atm_pct: float = DEFAULT_ATM_PCT,
    max_rows: int | None = DEFAULT_MAX_ROWS,
    uninvertible: str = "skip",
    thresholds: Thresholds | None = None,
) -> DriftReport:
    """Load as-of chain, run identity checks, attach vendor diagnostics."""
    thr = thresholds or DEFAULT_THRESHOLDS
    if thresholds is None or thresholds.atm_pct != atm_pct:
        thr = Thresholds(**{**asdict(thr), "atm_pct": atm_pct})
    if asof_ns is None:
        asof_ns = cutoff_asof_ns(dt, cutoff_et)
    counts = ChainCounts()
    table = greeks_asof(
        dt,
        asof_ns,
        r=r,
        data_root=data_root,
        roots=roots,
        crr_steps=crr_steps,
        spy_american_moneyness=spy_atm_pct,
        moneyness=atm_pct,
        uninvertible=uninvertible,  # type: ignore[arg-type]
        max_rows=max_rows,
        counts=counts,
    )
    return evaluate_drift(
        table,
        counts=counts,
        thresholds=thr,
        dt=dt,
        asof_ns=asof_ns,
        cutoff_et=cutoff_et,
        r=r,
        spy_atm_pct=spy_atm_pct,
        max_rows=max_rows,
    )


def _write_report(report: DriftReport, data_root: str | os.PathLike[str] | None) -> Path:
    path = meta_path(REPORT_NAME, data_root=data_root)
    path.write_text(json.dumps(report.to_dict(), indent=2, default=str) + "\n", encoding="utf-8")
    return path


def _alert_fail(
    report: dict[str, Any],
    *,
    file_vals: Mapping[str, str] | None,
    ping_url: str | None,
    autocreate: bool,
) -> None:
    ping(ping_url, "/fail", autocreate, body=json.dumps(report, default=str)[:10000])
    webhook = _get("DRIFT_ALERT_WEBHOOK", None, file_vals)
    if webhook:
        _post_webhook(webhook, report)


def main(argv: list[str] | None = None) -> int:
    """CLI: exit 0 on PASS, 1 on identity/vendor/data failure. Exit 0 on holidays."""
    file_vals = _dotenv()
    parser = argparse.ArgumentParser(
        prog="python -m pricing.drift_check",
        description=(
            "Daily identity canary on own IV/Greeks from the as-of chain. "
            "Vendor diffs are diagnostic when present; null vendor IV is not "
            "a FAIL. Exits 1 when identities break or data is missing."
        ),
    )
    parser.add_argument("--date", default=None, help="partition date YYYY-MM-DD (default: today ET)")
    parser.add_argument(
        "--asof-ns",
        type=int,
        default=None,
        help="as-of instant (ns epoch); default = last snapshot at or before --cutoff-et",
    )
    parser.add_argument(
        "--cutoff-et",
        default=DEFAULT_CUTOFF_ET,
        help=f"HH:MM America/New_York; last file at or before this clock (default {DEFAULT_CUTOFF_ET})",
    )
    parser.add_argument(
        "--r",
        type=float,
        default=None,
        help="continuous risk-free rate; default DRIFT_CHECK_R, else the "
             "landed Treasury curve interpolated to each contract's maturity",
    )
    parser.add_argument("--roots", default=",".join(ALLOWED_ROOTS))
    parser.add_argument(
        "--data-root",
        default=None,
        help="warehouse root (default DATA_ROOT or /data/massive)",
    )
    parser.add_argument("--crr-steps", type=int, default=CHAIN_CRR_STEPS)
    parser.add_argument(
        "--spy-atm-pct",
        type=float,
        default=DEFAULT_SPY_ATM_PCT,
        help="American CRR only for SPY strikes within this fraction of F; rest is European",
    )
    parser.add_argument(
        "--atm-pct",
        type=float,
        default=DEFAULT_ATM_PCT,
        help="price and compare only |K/F-1| ≤ this (default 0.05)",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=DEFAULT_MAX_ROWS,
        help=f"cap American CRR rows (default {DEFAULT_MAX_ROWS}; not a global row cap)",
    )
    parser.add_argument(
        "--min-compare",
        type=int,
        default=DEFAULT_MIN_COMPARE,
        help=(
            "min ATM names for identity tests; also the min vendor-comparable "
            "ATM names before vendor drift can FAIL"
        ),
    )
    parser.add_argument("--fail-frac", type=float, default=DEFAULT_FAIL_FRAC)
    parser.add_argument("--iv-median-abs", type=float, default=DEFAULT_THRESHOLDS.iv_median_abs)
    parser.add_argument("--force", action="store_true", help="run even on closed days")
    parser.add_argument("--dry-run", action="store_true", help="do not write _meta/drift_check.json")
    parser.set_defaults(uninvertible="skip")
    parser.add_argument(
        "--skip-uninvertible",
        dest="uninvertible",
        action="store_const",
        const="skip",
        help="omit uninvertible / missing-forward rows (default)",
    )
    parser.add_argument(
        "--raise-uninvertible",
        dest="uninvertible",
        action="store_const",
        const="raise",
        help="fail loud on the first uninvertible row",
    )
    args = parser.parse_args(argv)

    data_root = args.data_root or _get("DATA_ROOT", "/data/massive", file_vals)
    log_root = _get("LOG_ROOT", None, file_vals) or str(Path(data_root) / "logs")
    dt = date.fromisoformat(args.date) if args.date else today_et()

    logger = get_run_logger(JOB, dt, log_root=log_root)
    ping_url, autocreate = _hc_target(file_vals)
    ping(ping_url, "/start", autocreate)
    # ``cli.run_job`` guarantees exactly one terminal event and one terminal
    # ping per run. This job does not go through it, and the exception
    # taxonomy caught below is narrow: on 2026-09-01 the 17:00 cron run logged
    # ``job_start`` and then nothing at all -- no job_end, no job_error, no
    # ping -- because whatever ended it was outside that taxonomy. A canary
    # that can die without saying so is not a canary, so every exit path from
    # here on is accounted for, including the ones nobody enumerated.
    #
    # Config validation is inside the guard, not before it: a bad
    # DRIFT_CHECK_R or --roots used to return 1 before the logger or the
    # /start ping existed, so the run that never happened looked identical to
    # a run that was never scheduled.
    sent_terminal = False
    try:
        r = float(args.r) if args.r is not None else default_r(file_vals)
        roots = narrow_roots(tuple(args.roots.split(",")))
        if not args.force:
            try:
                require_trading_day(dt, force=False, data_root=data_root)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
                if code == 0:
                    logger.log("job_end", job=JOB, skipped="not_a_trading_day", date=dt.isoformat())
                    sent_terminal = True
                    ping(ping_url, "", autocreate, body=f"{JOB} skipped (not a trading day)")
                    return 0
                raise

        logger.log(
            "job_start",
            job=JOB,
            date=dt.isoformat(),
            r=r,
            cutoff_et=args.cutoff_et,
            spy_atm_pct=args.spy_atm_pct,
            atm_pct=args.atm_pct,
            max_rows=args.max_rows,
        )
        report = run_drift(
            dt,
            r=r,
            data_root=data_root,
            asof_ns=args.asof_ns,
            cutoff_et=args.cutoff_et,
            roots=roots,
            crr_steps=args.crr_steps,
            spy_atm_pct=args.spy_atm_pct,
            atm_pct=args.atm_pct,
            max_rows=args.max_rows,
            uninvertible=args.uninvertible,
            thresholds=Thresholds(
                min_compare=args.min_compare,
                fail_frac=args.fail_frac,
                iv_median_abs=args.iv_median_abs,
                atm_pct=args.atm_pct,
            ),
        )
        payload = report.to_dict()
        logger.log("drift", **{k: v for k, v in payload.items() if k != "units"})
        if report.vendor_compare_skipped:
            logger.log(
                "vendor_compare_skipped",
                vendor_compared=report.counts.get("vendor_compared"),
                no_vendor_iv=report.counts.get("no_vendor_iv"),
                min_compare=report.thresholds.get("min_compare"),
            )
        print(_render(report), file=sys.stderr)
        if not args.dry_run:
            path = _write_report(report, data_root)
            print(f"{report.status}  wrote {path}", file=sys.stderr)
        if report.status == FAIL:
            logger.log("job_end", job=JOB, status=FAIL, date=dt.isoformat())
            sent_terminal = True
            _alert_fail(payload, file_vals=file_vals, ping_url=ping_url, autocreate=autocreate)
            return 1
        logger.log("job_end", job=JOB, status=report.status, date=dt.isoformat())
        sent_terminal = True
        ping(ping_url, "", autocreate, body=f"{JOB} ok: {report.counts}")
        return 0
    except (CatalogError, SchemaError, ChainError, DriftError, ValueError, OSError) as exc:
        logger.log("job_error", job=JOB, error=f"{type(exc).__name__}: {exc}")
        print(f"FAIL  drift  {exc}", file=sys.stderr)
        stub = {
            "date": dt.isoformat(),
            "status": FAIL,
            "failures": [str(exc)],
            "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        }
        if not args.dry_run:
            try:
                path = meta_path(REPORT_NAME, data_root=data_root)
                path.write_text(json.dumps(stub, indent=2) + "\n", encoding="utf-8")
            except OSError as write_exc:
                print(f"warning: could not write report: {write_exc}", file=sys.stderr)
        sent_terminal = True
        _alert_fail(stub, file_vals=file_vals, ping_url=ping_url, autocreate=autocreate)
        return 1
    except BaseException as exc:
        # Deliberately broad, and re-raised: MemoryError, KeyboardInterrupt, a
        # SIGTERM-turned-SystemExit and every bug outside the taxonomy above
        # used to leave the run silent. Report, then let it propagate.
        logger.log("job_error", job=JOB, error=f"{type(exc).__name__}: {exc}",
                   unhandled=True)
        print(f"FAIL  drift  unhandled {type(exc).__name__}: {exc}", file=sys.stderr)
        sent_terminal = True
        ping(ping_url, "/fail", autocreate,
             body=f"{JOB} unhandled {type(exc).__name__}: {exc}"[:10000])
        raise
    finally:
        if not sent_terminal:
            # Reached only if the interpreter left the try block by a route
            # neither handler saw. Still better than silence.
            logger.log("job_end", job=JOB, status=FAIL, date=dt.isoformat(),
                       error="run ended without a terminal event")
            ping(ping_url, "/fail", autocreate,
                 body=f"{JOB} ended without a terminal event")
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
