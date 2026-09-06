"""Drift tests for the hand-maintained HTML handbook in docs/.

The docs pages are written by hand, so nothing regenerates them when the
schedule, the environment variables, or the box layout change. These tests
fail CI when deploy/schedule.json, .env.example, or the page cross-links move
without the handbook being updated to match.

They also pin a few facts that have already gone stale in prose while CI
stayed green — allowlisted roots, the canary rate default, the American IV
solver. Substring checks, not NLP: if you rename a page, update the pin.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"

# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _schedule_units() -> list[dict]:
    """Unit entries from deploy/schedule.json -- the canonical schedule."""
    return json.loads((REPO_ROOT / "deploy" / "schedule.json").read_text())["units"]


def _scheduled_ingest_jobs() -> set[str]:
    """Job modules run via `-m ingest.jobs.<name>` per deploy/schedule.json."""
    jobs = set()
    for unit in _schedule_units():
        cmd = unit["command"]
        if cmd[0] == "-m" and cmd[1].startswith("ingest.jobs."):
            jobs.add(cmd[1].removeprefix("ingest.jobs."))
    return jobs


def _env_example_vars() -> set[str]:
    """Variable names defined in .env.example (comments and blanks skipped)."""
    names = set()
    for line in (REPO_ROOT / ".env.example").read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"([A-Z][A-Z0-9_]*)=", line)
        if m:
            names.add(m.group(1))
    return names


def _doc_pages() -> dict[str, str]:
    """Every handbook page, name -> text."""
    return {p.name: p.read_text() for p in sorted(DOCS_DIR.glob("*.html"))}


def _local_hrefs(text: str) -> list[str]:
    """href targets that stay inside the handbook (external links skipped)."""
    return [
        href
        for href in re.findall(r'href="([^"]+)"', text)
        if not href.startswith(("http://", "https://"))
    ]


# ---------------------------------------------------------------------------
# Scheduled jobs <-> ingest doc page
# ---------------------------------------------------------------------------


def test_scheduled_jobs_documented_in_ingest_page() -> None:
    """A scheduled job missing from the ingest page is invisible to the desk."""
    ingest = (DOCS_DIR / "ingest.html").read_text()
    missing = {job for job in _scheduled_ingest_jobs() if job not in ingest}
    assert not missing, f"scheduled jobs not documented in docs/ingest.html: {sorted(missing)}"


def test_drift_check_documented_in_ingest_page() -> None:
    """pricing.drift_check runs from the same schedule and must be findable."""
    assert any(u["command"][:2] == ["-m", "pricing.drift_check"] for u in _schedule_units())
    ingest = (DOCS_DIR / "ingest.html").read_text()
    assert "drift_check" in ingest


def test_drift_check_has_canary_page() -> None:
    assert (DOCS_DIR / "canary.html").is_file()


# ---------------------------------------------------------------------------
# Env vars <-> knobs page
# ---------------------------------------------------------------------------


def test_env_example_vars_documented_in_knobs_page() -> None:
    """Every knob the box can be given must be in the documented-knobs page."""
    knobs = (DOCS_DIR / "knobs.html").read_text()
    missing = {name for name in _env_example_vars() if name not in knobs}
    assert not missing, f".env.example vars not documented in docs/knobs.html: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Handbook internal links resolve
# ---------------------------------------------------------------------------


def test_handbook_page_links_resolve() -> None:
    """Every link to another handbook page must point at a file that exists."""
    broken = []
    for name, text in _doc_pages().items():
        for href in _local_hrefs(text):
            if href.startswith("#"):
                continue
            target = href.split("#", 1)[0]
            if not (DOCS_DIR / target).is_file():
                broken.append(f"{name} -> {href}")
    assert not broken, f"links to missing files: {broken}"


def test_handbook_anchor_links_resolve() -> None:
    """A cross-page link with an anchor must land on a real id in the target."""
    pages = _doc_pages()
    broken = []
    for name, text in pages.items():
        for href in _local_hrefs(text):
            if href.startswith("#") or "#" not in href:
                continue
            target, anchor = href.split("#", 1)
            if target in pages and f'id="{anchor}"' not in pages[target]:
                broken.append(f"{name} -> {href}")
    assert not broken, f"links to missing anchors: {broken}"


def test_handbook_in_page_anchors_resolve() -> None:
    """An in-page href=\"#anchor\" must exist as an id in the same file."""
    broken = []
    for name, text in _doc_pages().items():
        for href in _local_hrefs(text):
            if href.startswith("#") and f'id="{href[1:]}"' not in text:
                broken.append(f"{name} -> {href}")
    assert not broken, f"in-page anchors with no matching id: {broken}"


# ---------------------------------------------------------------------------
# Stylesheet link
# ---------------------------------------------------------------------------


def test_pages_link_site_css() -> None:
    """Every handbook page shares the handbook stylesheet."""
    missing = [name for name, text in _doc_pages().items() if 'href="site.css"' not in text]
    assert not missing, f"pages not linking site.css: {missing}"


def test_pages_use_shared_shell() -> None:
    """Every handbook page links site.css and carries the shared nav.toc.

    404.html is a standalone error page: it must still link site.css but is
    exempt from the nav requirement.
    """
    missing_css = []
    missing_nav = []
    for name, text in _doc_pages().items():
        if 'href="site.css"' not in text:
            missing_css.append(name)
        if name != "404.html" and 'class="toc"' not in text:
            missing_nav.append(name)
    assert not missing_css, f"pages not linking site.css: {missing_css}"
    assert not missing_nav, f"pages without nav.toc: {missing_nav}"


# ---------------------------------------------------------------------------
# Box path consistency
# ---------------------------------------------------------------------------


def test_box_path_matches_ops_page() -> None:
    """The checkout name the generated units cd into must match the ops page."""
    template = (REPO_ROOT / "deploy" / "ansible" / "templates" / "massive-job.service.j2").read_text()
    m = re.search(r"^WorkingDirectory=%h/(\S+)$", template, re.MULTILINE)
    assert m, "massive-job.service.j2 must set WorkingDirectory=%h/<checkout>"
    checkout = m.group(1)
    ops = (DOCS_DIR / "box-operations.html").read_text()
    assert checkout in ops, f"checkout name {checkout!r} not in docs/box-operations.html"


# ---------------------------------------------------------------------------
# Facts that have already gone stale in prose
# ---------------------------------------------------------------------------


def test_every_scheduled_job_named_on_ingest_page() -> None:
    """ingest.html must name every schedule.json job, not just ingest.jobs.*."""
    ingest = (DOCS_DIR / "ingest.html").read_text()
    missing = sorted({u["job"] for u in _schedule_units() if u["job"] not in ingest})
    assert not missing, f"scheduled jobs not named in docs/ingest.html: {missing}"


def test_opra_allowlist_on_marketdata_page() -> None:
    """The marketdata page must list every live root. Caught the VIX miss."""
    from ingest.jobs import OPTION_ROOTS
    from marketdata.opra import ALLOWED_ROOTS

    assert OPTION_ROOTS == ALLOWED_ROOTS
    text = (DOCS_DIR / "marketdata.html").read_text()
    missing = [root for root in ALLOWED_ROOTS if root not in text]
    assert not missing, f"ALLOWED_ROOTS missing from docs/marketdata.html: {missing}"


def test_surface_roots_on_pricing_page() -> None:
    from pricing.surface import SURFACE_ROOTS

    text = (DOCS_DIR / "pricing.html").read_text()
    missing = [root for root in SURFACE_ROOTS if root not in text]
    assert not missing, f"SURFACE_ROOTS missing from docs/pricing.html: {missing}"
    assert "implied_vol_american" in text


def test_surface_is_scheduled_after_the_atm_curve() -> None:
    """The smile is a derived reduction of the same T-1 day bars as the ATM curve."""
    units = {u["job"]: u for u in _schedule_units()}
    assert units["surface"]["unit"] == "massive-surface"
    assert units["surface"]["command"] == ["-m", "pricing.surface"]
    assert units["surface"]["cron"] == ["15 12 * * 2-6"]
    ingest = (DOCS_DIR / "ingest.html").read_text()
    pricing = (DOCS_DIR / "pricing.html").read_text()
    assert "12:15" in ingest and "surface" in ingest
    assert "12:15" in pricing
    assert "load_surface" in pricing
    assert "Deliberately unscheduled" not in pricing


def test_drift_check_r_is_override_not_the_curve(monkeypatch) -> None:
    """DRIFT_CHECK_R is optional; unset means the Treasury curve, not 0.04."""
    from pricing.drift_check import DEFAULT_R, default_r

    monkeypatch.delenv("DRIFT_CHECK_R", raising=False)
    assert DEFAULT_R == 0.04
    assert default_r({}) is None
    knobs = (DOCS_DIR / "knobs.html").read_text()
    canary = (DOCS_DIR / "canary.html").read_text()
    for name, text in (("knobs.html", knobs), ("canary.html", canary)):
        assert "DRIFT_CHECK_R" in text, name
        assert "Treasury curve" in text, f"{name} must say the canary defaults to the curve"


def test_canary_page_documents_reprice_solver_bands() -> None:
    """Issue #43: the handbook must not keep the old $0.05 nickel."""
    from pricing.drift_check import DEFAULT_THRESHOLDS

    text = (DOCS_DIR / "canary.html").read_text()
    assert "reprice_abs = 1e-3" in text
    assert "reprice_rel = 0</code>" in text
    assert "reprice_median_abs = 1e-4" in text
    assert "CHAIN_CRR_STEPS" in text
    assert DEFAULT_THRESHOLDS.reprice_abs == 1e-3
    assert DEFAULT_THRESHOLDS.reprice_rel == 0.0
    assert DEFAULT_THRESHOLDS.reprice_median_abs == 1e-4


def test_from_market_docstring_does_not_deny_american_iv() -> None:
    """A stale module docstring must not unteach implied_vol_american."""
    src = (REPO_ROOT / "pricing" / "from_market.py").read_text()
    assert "there is no American IV solver" not in src
    assert "implied_vol_american" in src
    # expiry_instant / module docstring must name VIX settlement, not just SPX.
    assert "VIX" in src.split("from __future__")[0]


def test_red_day_page_names_repair_and_snapshots() -> None:
    text = (DOCS_DIR / "red-day.html").read_text()
    assert "repair.sh" in text
    assert "snapshots cannot be repaired" in text
    assert "prune_raw.sh" in text
    assert 'id="red-early"' in text


def test_latent_page_points_at_improvements() -> None:
    assert (REPO_ROOT / "IMPROVEMENTS.md").is_file()
    text = (DOCS_DIR / "latent.html").read_text()
    assert "IMPROVEMENTS.md" in text
    assert "job_end" in text
    assert "deliberately unmonitored" in text


def test_handbook_nav_includes_red_day_and_latent() -> None:
    missing = []
    for name, text in _doc_pages().items():
        if name == "404.html":
            continue
        if "red-day.html" not in text:
            missing.append(f"{name} missing red-day.html")
        if "latent.html" not in text:
            missing.append(f"{name} missing latent.html")
    assert not missing, missing
