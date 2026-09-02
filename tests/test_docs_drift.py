"""Drift tests for the hand-maintained HTML handbook in docs/.

The docs pages are written by hand, so nothing regenerates them when the
schedule, the environment variables, or the box layout change. These tests
fail CI when deploy/crontab, .env.example, or the page cross-links move
without the handbook being updated to match.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"

# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _crontab_lines() -> list[str]:
    """Active (non-comment, non-blank) lines of deploy/crontab."""
    crontab = (REPO_ROOT / "deploy" / "crontab").read_text()
    return [
        line for line in crontab.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


def _scheduled_ingest_jobs() -> set[str]:
    """Job modules run via `$PY -m ingest.jobs.<name>` in deploy/crontab."""
    jobs = set()
    for line in _crontab_lines():
        jobs.update(re.findall(r"-m ingest\.jobs\.(\w+)", line))
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
# Crontab jobs <-> ingest doc page
# ---------------------------------------------------------------------------


def test_crontab_jobs_documented_in_ingest_page() -> None:
    """A scheduled job missing from the ingest page is invisible to the desk."""
    ingest = (DOCS_DIR / "ingest.html").read_text()
    missing = {job for job in _scheduled_ingest_jobs() if job not in ingest}
    assert not missing, f"scheduled jobs not documented in docs/ingest.html: {sorted(missing)}"


def test_drift_check_documented_in_ingest_page() -> None:
    """pricing.drift_check runs from the same crontab and must be findable."""
    assert any("-m pricing.drift_check" in line for line in _crontab_lines())
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
    """Every page except box-operations.html shares the handbook stylesheet."""
    missing = [
        name
        for name, text in _doc_pages().items()
        if name != "box-operations.html" and 'href="site.css"' not in text
    ]
    assert not missing, f"pages not linking site.css: {missing}"


def test_box_operations_uses_inline_style_not_site_css() -> None:
    """box-operations.html intentionally carries its own inline style.

    If it ever links site.css, the exception above stops being deliberate
    and both tests should be revisited together.
    """
    text = (DOCS_DIR / "box-operations.html").read_text()
    assert 'href="site.css"' not in text
    assert "<style" in text


# ---------------------------------------------------------------------------
# Box path consistency
# ---------------------------------------------------------------------------


def test_box_path_matches_ops_page() -> None:
    """The checkout name in crontab's REPO must match the documented box layout."""
    m = re.search(r"^REPO=(\S+)$", (REPO_ROOT / "deploy" / "crontab").read_text(), re.MULTILINE)
    assert m, "deploy/crontab must set REPO=<path>"
    checkout = Path(m.group(1)).name
    ops = (DOCS_DIR / "box-operations.html").read_text()
    assert checkout in ops, f"checkout name {checkout!r} not in docs/box-operations.html"
