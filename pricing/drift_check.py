"""Daily canary: own IV/Greeks vs vendor snapshot columns.

Vendor numbers are a **canary**, not gospel. A sudden jump in own-vs-vendor
delta or IV usually means the pipeline or the maths broke (schema drift, a
bad invert, a silent engine swap). Model disagreement on far OTM ticks is
expected and is not this job's trigger.

Unit alignment (mandatory)
--------------------------
Massive/Polygon snapshot greeks are **not** in this package's native units:

* ``implied_volatility``, ``delta``, ``gamma`` — same as ours (decimal vol,
  spot derivatives).
* ``theta`` — per **calendar day**. Ours is per **year**. Compare after
  ``vendor_theta * 365``.
* ``vega`` — per **1% vol**. Ours is per **1.00 vol**. Compare after
  ``vendor_vega * 100``.

Naive ``own_theta - vendor_theta`` will always look like a break.

Fail rule
---------
Only ATM names (``|K/F − 1| ≤ atm_pct``, default 5%) that carry vendor IV
enter the compare set. The job FAILs (exit 1) when any of:

* no data / schema / foreign roots / nothing priced
* fewer than ``min_compare`` ATM names with vendor IV
* median ``|ΔIV|`` on that set exceeds ``iv_median_abs`` (default 4 vol pts)
* the fraction of ATM names with any core greek (IV, delta, vega, gamma,
  theta) beyond its abs/rel band exceeds ``fail_frac`` (default 25%)

A name is beyond band on a greek when
``|own − vendor_aligned| > max(abs, rel * max(|own|, |vendor_aligned|))``.

Cron bounds
-----------
SPY CRR is limited with ``--spy-atm-pct`` (default 5%) and ``--max-rows``
(default 400). Combined with ``--atm-pct``, only the ATM slice is inverted.
``--r`` defaults to ``DRIFT_CHECK_R`` (else 0.04); there is no rates warehouse.

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

from ingest.common.cli import healthcheck_slug, ping
from ingest.common.config import _parse_env_file
from ingest.common.landing import meta_path
from ingest.common.logging_utils import get_run_logger
from ingest.common.market_gate import require_trading_day, today_et
from marketdata.catalog import CatalogError, SchemaError
from marketdata.opra import ALLOWED_ROOTS
from marketdata.validate import narrow_roots
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
    """Abs + rel bands and the ATM-set fail rule."""

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
    r: float
    status: str
    failures: list[str] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)
    median_abs_iv: float | None = None
    frac_beyond: float | None = None
    beyond_by_greek: dict[str, int] = field(default_factory=dict)
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


def evaluate_drift(
    table: pa.Table,
    *,
    counts: ChainCounts | None = None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    dt: date,
    asof_ns: int | None,
    cutoff_et: str = DEFAULT_CUTOFF_ET,
    r: float,
    spy_atm_pct: float | None = None,
    max_rows: int | None = None,
) -> DriftReport:
    """Apply the ATM median-IV / beyond-band rule to a priced chain."""
    rows = table.to_pylist()
    atm: list[dict[str, Any]] = []
    n_no_vendor = 0
    for rec in rows:
        if not is_atm(rec, thresholds.atm_pct):
            continue
        if _opt(rec.get("vendor_iv")) is None:
            n_no_vendor += 1
            continue
        atm.append(rec)

    failures: list[str] = []
    beyond_by_greek = dict.fromkeys(CORE_GREEKS, 0)
    n_beyond = 0
    iv_abs: list[float] = []

    for rec in atm:
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
            n_beyond += 1

    median_iv = _median(iv_abs) if iv_abs else None
    frac = (n_beyond / len(atm)) if atm else None

    if len(atm) < thresholds.min_compare:
        failures.append(
            f"only {len(atm)} ATM names with vendor IV "
            f"(need ≥ {thresholds.min_compare})"
        )
    if median_iv is not None and median_iv > thresholds.iv_median_abs:
        failures.append(
            f"median |ΔIV|={median_iv:.4f} exceeds {thresholds.iv_median_abs}"
        )
    if frac is not None and frac > thresholds.fail_frac:
        failures.append(
            f"{n_beyond}/{len(atm)} ATM names beyond band "
            f"({frac:.0%} > {thresholds.fail_frac:.0%})"
        )

    chain_counts = {
        "quotes": counts.n_quotes if counts else None,
        "priced": counts.n_priced if counts else table.num_rows,
        "expired": counts.n_expired if counts else None,
        "no_price": counts.n_no_price if counts else None,
        "uninvertible": counts.n_uninvertible if counts else None,
        "otm": counts.n_otm if counts else None,
        "atm_compared": len(atm),
        "no_vendor_iv": n_no_vendor,
        "beyond_band": n_beyond,
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
        frac_beyond=frac,
        beyond_by_greek=beyond_by_greek,
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
    lines = [
        f"drift_check -- {report.date}  asof_ns={report.asof_ns}  "
        f"cutoff_et={report.cutoff_et} ET",
        "-" * 72,
        f"{report.status:<5} median_|ΔIV|={report.median_abs_iv}  "
        f"frac_beyond={report.frac_beyond}  "
        f"atm={report.counts.get('atm_compared')}",
        f"      priced={report.counts.get('priced')}  "
        f"expired={report.counts.get('expired')}  "
        f"no_price={report.counts.get('no_price')}  "
        f"uninvertible={report.counts.get('uninvertible')}  "
        f"otm={report.counts.get('otm')}",
        f"      beyond_by_greek={report.beyond_by_greek}",
        f"      units: vendor theta * {VENDOR_THETA_TO_YEAR:g} (day→year), "
        f"vendor vega * {VENDOR_VEGA_TO_PER_1:g} (1%→1.00)",
    ]
    for msg in report.failures:
        lines.append(f"FAIL  {msg}")
    lines.append("-" * 72)
    return "\n".join(lines)


def _dotenv() -> dict[str, str]:
    return _parse_env_file(Path(".env"))


def _get(name: str, default: str | None = None, file_vals: Mapping[str, str] | None = None) -> str | None:
    if name in os.environ:
        return os.environ[name]
    if file_vals and name in file_vals:
        return file_vals[name]
    return default


def default_r(file_vals: Mapping[str, str] | None = None) -> float:
    raw = _get("DRIFT_CHECK_R", None, file_vals)
    if raw is None or raw == "":
        return DEFAULT_R
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
    url = _get("HEALTHCHECKS_PING_URL", None, file_vals)
    return (url or None), False


def _post_webhook(url: str, payload: dict[str, Any]) -> None:
    """Best-effort POST of the FAIL report. Never raises."""
    try:
        data = json.dumps(payload, default=str).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=5)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"warning: drift webhook failed: {exc}", file=sys.stderr)


def run_drift(
    dt: date,
    *,
    r: float,
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
    """Load as-of chain, compare aligned vendor diagnostics, return a report."""
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
    """CLI: exit 0 on PASS, 1 on drift / no data / schema. Exit 0 on holidays."""
    file_vals = _dotenv()
    parser = argparse.ArgumentParser(
        prog="python -m pricing.drift_check",
        description=(
            "Daily own-vs-vendor IV/Greeks canary. Vendor is a canary, not "
            "gospel. Theta/vega are unit-aligned before compare. Exits 1 on "
            "drift so cron/Healthchecks can alert."
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
        help=f"continuous risk-free rate (default DRIFT_CHECK_R or {DEFAULT_R})",
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
        help=f"CRR/cron budget (default {DEFAULT_MAX_ROWS})",
    )
    parser.add_argument("--min-compare", type=int, default=DEFAULT_MIN_COMPARE)
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
    try:
        r = float(args.r) if args.r is not None else default_r(file_vals)
        roots = narrow_roots(tuple(args.roots.split(",")))
    except (ValueError, DriftError) as exc:
        print(f"FAIL  drift  {exc}", file=sys.stderr)
        return 1

    logger = get_run_logger(JOB, dt, log_root=log_root)
    ping_url, autocreate = _hc_target(file_vals)
    ping(ping_url, "/start", autocreate)
    try:
        if not args.force:
            try:
                require_trading_day(dt, force=False, data_root=data_root)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
                if code == 0:
                    logger.log("job_end", job=JOB, skipped="not_a_trading_day", date=dt.isoformat())
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
        print(_render(report), file=sys.stderr)
        if not args.dry_run:
            path = _write_report(report, data_root)
            print(f"{report.status}  wrote {path}", file=sys.stderr)
        if report.status == FAIL:
            _alert_fail(payload, file_vals=file_vals, ping_url=ping_url, autocreate=autocreate)
            return 1
        ping(ping_url, "", autocreate, body=f"{JOB} ok: {report.counts}")
        return 0
    except (CatalogError, SchemaError, ChainError, DriftError, ValueError) as exc:
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
        _alert_fail(stub, file_vals=file_vals, ping_url=ping_url, autocreate=autocreate)
        return 1
    finally:
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
