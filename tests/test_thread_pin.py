"""BLAS threads are pinned, because the SVI fit is not reproducible without it.

Measured on 2026-09-04: the same code and inputs at 1 thread against 8 moved
397 of 720 ``vol_surface`` values, ``svi_rho`` by up to 2.24e-03 relative,
while ``rms_error`` moved less than 1e-09. The optimiser lands elsewhere in an
equally good basin, so the fit is not wrong -- it is just not the same numbers
twice, which is enough to make a rebuild undiffable and the backtester's inputs
unreproducible.

The count is 8. Reproducibility is what the pin buys and any fixed count buys
it, so the value is chosen on solver behaviour: rebuilding the archive at 1
thread on 2026-09-08 tripped the butterfly or calendar arbitrage guard on 27
of the 660 sessions that fit cleanly at 8, and 8 of those 27 landed with
``svi_b`` at its upper bound -- the solve hit the boundary instead of finding
a fit. ``scripts/cronjob.sh`` carries the full measurement.

These tests pin the mechanism rather than the numerics: the value has to reach
the job's own process, it has to be the same value in every writer that sets
one, in the archive rebuild it has to be set before OpenBLAS loads, and the
value stamped on landed ``vol_surface`` rows (``blas_threads``) has to be the
one the job actually ran under.

What they deliberately do not assert: that a writer cannot run at some other
count. ``setdefault`` and ``:=`` both yield to an inherited value, which is the
override ``blas_threads`` exists to record. They check the source-level
agreement of the three writers, not their runtime environment.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CRONJOB = REPO / "scripts" / "cronjob.sh"
BUILD_SURFACE = REPO / "scripts" / "build_surface.py"
BUILD_RV = REPO / "scripts" / "build_rv_forecast.py"

# The one number every writer has to agree on.
PIN = "8"

THREAD_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)

PROBE = (
    "import os;print(' '.join("
    f"f'{{v}}={{os.environ.get(v)}}' for v in {list(THREAD_VARS)!r}))"
)


def _run_under_cronjob(env_extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in THREAD_VARS}
    env.update(env_extra or {})
    out = subprocess.run(
        ["bash", str(CRONJOB), "pintest", sys.executable, "-c", PROBE],
        capture_output=True, text=True, env=env, timeout=60, check=True,
    ).stdout
    return dict(part.split("=", 1) for part in out.split())


@pytest.mark.parametrize("var", THREAD_VARS)
def test_cronjob_pins_every_thread_var(var: str) -> None:
    """Every scheduled job runs through cronjob.sh, so the pin belongs there
    rather than in one unit -- it covers the systemd timers and the crontab
    fallback with one definition."""
    assert _run_under_cronjob()[var] == PIN


def test_cronjob_leaves_a_deliberate_override_alone() -> None:
    """``:=`` not ``=``. The units never set these, so scheduled runs always
    get the pin; an operator benchmarking by hand keeps their choice."""
    assert _run_under_cronjob({"OMP_NUM_THREADS": "4"})["OMP_NUM_THREADS"] == "4"


def test_build_surface_pins_before_numpy_can_load() -> None:
    """OpenBLAS reads its thread count once, when the shared library loads.

    ``scripts/build_surface.py`` is run directly rather than through
    cronjob.sh, so it sets the vars itself -- and it has to do so above the
    ``pricing`` import, or numpy is already in memory and the assignment is
    silently a no-op. This asserts the ordering, which is the part a later edit
    would plausibly break.
    """
    src = BUILD_SURFACE.read_text()
    pin_at = src.index("OPENBLAS_NUM_THREADS")
    import_at = min(
        src.index(line) for line in ("from pricing.surface import", "import numpy")
        if line in src
    )
    assert pin_at < import_at, (
        "build_surface.py sets the BLAS thread pin after importing pricing; "
        "OpenBLAS has already read its thread count by then"
    )
    for var in THREAD_VARS:
        assert var in src


@pytest.mark.parametrize("script", [BUILD_SURFACE, BUILD_RV])
def test_manual_rebuilds_pin_the_same_count_as_the_scheduled_job(script: Path) -> None:
    """Three writers set this independently -- cronjob.sh for every scheduled
    job, and the two archive rebuild scripts that are run by hand -- so the
    value can drift apart in the source without anything noticing.

    For ``vol_surface`` that would mix two non-comparable fits into one
    archive, visibly, because every row stamps the count it ran under. For
    ``rv_forecast`` there is no stamp and no scheduled writer to agree with at
    all (see scripts/build_rv_forecast.py); this only keeps the repo's own two
    values from diverging.
    """
    src = script.read_text()
    values = set(re.findall(r'os\.environ\.setdefault\(_var, "(\d+)"\)', src))
    assert values == {PIN}, f"{script.name} pins {values or 'nothing'}, expected {PIN}"


def test_cronjob_and_the_rebuild_scripts_do_not_disagree() -> None:
    """The bash side of the same contract, read from the file rather than run,
    so a mismatch is named here instead of surfacing as an undiffable archive.
    """
    shell = CRONJOB.read_text()
    values = set(re.findall(r':\s*"\$\{[A-Z_]+:=(\d+)\}"', shell))
    assert values == {PIN}, f"cronjob.sh pins {values or 'nothing'}, expected {PIN}"


PIN_STAMP_PROBE = (
    f"import sys;sys.path.insert(0, {str(REPO)!r});"
    "from pricing.surface import blas_thread_pin;print(blas_thread_pin())"
)


def test_cronjob_pin_is_what_gets_stamped_on_rows() -> None:
    """Every vol_surface row stamps ``blas_threads``; the stamp is fiction if
    the value recorded is not the one the scheduled job runs under."""
    env = {k: v for k, v in os.environ.items() if k not in THREAD_VARS}
    out = subprocess.run(
        ["bash", str(CRONJOB), "pintest", sys.executable, "-c", PIN_STAMP_PROBE],
        capture_output=True, text=True, env=env, timeout=60, check=True,
    ).stdout.strip()
    assert out == PIN


def test_a_deliberate_override_is_what_gets_stamped() -> None:
    """cronjob.sh leaves an operator's override in place; the stamp records
    the count the fit actually ran under, not the scheduled default."""
    env = {k: v for k, v in os.environ.items() if k not in THREAD_VARS}
    env["OPENBLAS_NUM_THREADS"] = "4"
    out = subprocess.run(
        ["bash", str(CRONJOB), "pintest", sys.executable, "-c", PIN_STAMP_PROBE],
        capture_output=True, text=True, env=env, timeout=60, check=True,
    ).stdout.strip()
    assert out == "4"
