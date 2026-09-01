"""Vendor greeks/IV are diagnostics on Quote and unused as pricing inputs."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ingest.schemas import flatten_snapshot
from marketdata.types import quotes_from_snapshot_rows
from pricing.conventions import GreeksConventions
from pricing.from_market import greeks_quote, price_quote
from tests.conftest import load_fixture
from tests.marketdata.conftest import snapshot_row

REPO = Path(__file__).resolve().parents[2]
SPOT = GreeksConventions(
    vega_unit="per_1.00",
    theta_unit="per_year",
    delta_kind="spot",
    gamma_kind="spot",
)


def test_quotes_carry_vendor_columns() -> None:
    rec = flatten_snapshot(load_fixture("snapshot_options_spy.json")["results"][0])
    for name in (
        "greeks_delta",
        "greeks_gamma",
        "greeks_theta",
        "greeks_vega",
        "implied_volatility",
    ):
        assert name in rec
    quotes = quotes_from_snapshot_rows(
        [snapshot_row("O:SPY260831C00420000", vendor_iv=0.22, vendor_delta=0.55)]
    )
    q = quotes[0]
    assert q.vendor_implied_volatility == pytest.approx(0.22)
    assert q.vendor_delta == pytest.approx(0.55)


def test_poisoned_vendor_iv_does_not_change_price() -> None:
    base = snapshot_row("O:SPY260831C00420000", vendor_iv=0.12, vendor_delta=0.4)
    poison = snapshot_row("O:SPY260831C00420000", vendor_iv=9.99, vendor_delta=-1.0)
    q0, q1 = quotes_from_snapshot_rows([base])[0], quotes_from_snapshot_rows([poison])[0]
    kwargs = {"r": 0.05, "sigma": 0.20, "q": 0.01}
    assert price_quote(q0, **kwargs) == price_quote(q1, **kwargs)
    g0 = greeks_quote(q0, conventions=SPOT, **kwargs)
    g1 = greeks_quote(q1, conventions=SPOT, **kwargs)
    assert g0.delta == g1.delta
    assert g0.vega == g1.vega


_BANNED_VENDOR_TOKENS = (
    "vendor_implied_volatility",
    "vendor_delta",
    "greeks_delta",
    "greeks_gamma",
    "greeks_theta",
    "greeks_vega",
    "implied_volatility",
)
_CALCULATOR_MODULES = ("bsm.py", "iv.py", "engine.py", "conventions.py")
_QUOTE_GLUE = (
    "engine_for",
    "expiry_instant",
    "year_fraction",
    "price_quote",
    "greeks_quote",
    "implied_vol_quote",
    "_spot_t_cp",
)


def test_calculator_modules_do_not_mention_vendor_columns() -> None:
    root = REPO / "pricing"
    for name in _CALCULATOR_MODULES:
        text = (root / name).read_text(encoding="utf-8")
        for token in _BANNED_VENDOR_TOKENS:
            assert token not in text, f"{name} mentions {token}"


def test_quote_glue_does_not_use_vendor_as_inputs() -> None:
    """Single-quote helpers stay vendor-blind; the chain copies diagnostics after."""
    path = REPO / "pricing" / "from_market.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    glue: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _QUOTE_GLUE:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            glue.append(segment)
    assert len(glue) == len(_QUOTE_GLUE)
    blob = "\n".join(glue)
    for token in _BANNED_VENDOR_TOKENS:
        assert token not in blob, f"quote glue mentions {token}"


def test_ingest_does_not_import_numpy_or_scipy() -> None:
    ingest = REPO / "ingest"
    for path in ingest.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in {"numpy", "scipy", "np"}
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in {"numpy", "scipy"}
