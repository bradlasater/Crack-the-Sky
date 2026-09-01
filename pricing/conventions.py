"""Desk conventions for the Greek catalog. Defaults are documented here.

Every test that reads a Greek must name the convention it is using.

Defaults
--------
* Vega is **per 1.00 vol** (a 1.00 change in σ, not a 1% move). Per-1% is
  ``vega_1.00 * 0.01``.
* Theta is **per year** of calendar time (T decreasing). Per calendar day
  divides by 365; per trading day divides by 252.
* ``delta`` / ``gamma`` are **spot** derivatives (∂/∂S). Dual (∂/∂K) is
  always also populated as ``dual_delta`` / ``dual_gamma``; setting
  ``delta_kind="dual"`` makes ``delta`` itself the strike derivative.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

VegaUnit = Literal["per_1.00", "per_1pct"]
ThetaUnit = Literal["per_year", "per_calendar_day", "per_trading_day"]
DeltaKind = Literal["spot", "dual"]
GammaKind = Literal["spot", "dual"]

CALENDAR_DAYS_PER_YEAR = 365
TRADING_DAYS_PER_YEAR = 252

GREEK_NAMES: tuple[str, ...] = (
    "price",
    "delta",
    "dual_delta",
    "vega",
    "theta",
    "rho",
    "rho_dividend",
    "gamma",
    "dual_gamma",
    "vanna",
    "volga",
    "charm",
    "veta",
    "vera",
    "speed",
    "zomma",
    "color",
    "ultima",
    "elasticity",
)


@dataclass(frozen=True, slots=True)
class GreeksConventions:
    """Frozen unit and kind choices applied when packing a catalog."""

    vega_unit: VegaUnit = "per_1.00"
    theta_unit: ThetaUnit = "per_year"
    delta_kind: DeltaKind = "spot"
    gamma_kind: GammaKind = "spot"
    calendar_days: int = CALENDAR_DAYS_PER_YEAR
    trading_days: int = TRADING_DAYS_PER_YEAR


# Documented defaults — tests that want them still pass this object by name.
DEFAULT_CONVENTIONS = GreeksConventions()


@dataclass(frozen=True, slots=True)
class GreeksCatalog:
    """Named first-, second-, and standard third-order Greeks plus price."""

    conventions: GreeksConventions
    price: Any
    delta: Any
    dual_delta: Any
    vega: Any
    theta: Any
    rho: Any
    rho_dividend: Any
    gamma: Any
    dual_gamma: Any
    vanna: Any
    volga: Any
    charm: Any
    veta: Any
    vera: Any
    speed: Any
    zomma: Any
    color: Any
    ultima: Any
    elasticity: Any

    def as_dict(self) -> dict[str, Any]:
        """Catalog as a dict; includes ``lambda`` (elasticity) and ``vomma``."""
        out = {name: getattr(self, name) for name in GREEK_NAMES}
        out["lambda"] = self.elasticity
        out["vomma"] = self.volga
        return out


def apply_conventions(
    raw: dict[str, Any],
    conventions: GreeksConventions,
) -> GreeksCatalog:
    """Scale raw (per 1.00 vol, per year, spot) Greeks by ``conventions``."""
    vol_scale = 0.01 if conventions.vega_unit == "per_1pct" else 1.0
    if conventions.theta_unit == "per_calendar_day":
        time_scale = 1.0 / conventions.calendar_days
    elif conventions.theta_unit == "per_trading_day":
        time_scale = 1.0 / conventions.trading_days
    else:
        time_scale = 1.0

    vega = raw["vega"] * vol_scale
    vanna = raw["vanna"] * vol_scale
    volga = raw["volga"] * (vol_scale**2)
    zomma = raw["zomma"] * vol_scale
    ultima = raw["ultima"] * (vol_scale**3)
    vera = raw["vera"] * vol_scale
    theta = raw["theta"] * time_scale
    charm = raw["charm"] * time_scale
    veta = raw["veta"] * time_scale * vol_scale
    color = raw["color"] * time_scale

    delta = raw["dual_delta"] if conventions.delta_kind == "dual" else raw["delta"]
    gamma = raw["dual_gamma"] if conventions.gamma_kind == "dual" else raw["gamma"]

    return GreeksCatalog(
        conventions=conventions,
        price=raw["price"],
        delta=delta,
        dual_delta=raw["dual_delta"],
        vega=vega,
        theta=theta,
        rho=raw["rho"],
        rho_dividend=raw["rho_dividend"],
        gamma=gamma,
        dual_gamma=raw["dual_gamma"],
        vanna=vanna,
        volga=volga,
        charm=charm,
        veta=veta,
        vera=vera,
        speed=raw["speed"],
        zomma=zomma,
        color=color,
        ultima=ultima,
        elasticity=raw["elasticity"],
    )
