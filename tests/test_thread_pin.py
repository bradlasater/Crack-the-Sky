"""BLAS threads are pinned, because the SVI fit is not reproducible without it.

Measured on 2026-09-04: the same code and inputs at 1 thread against 8 moved
397 of 720 ``vol_surface`` values, ``svi_rho`` by up to 2.24e-03 relative,
while ``rms_error`` moved less than 1e-09. The optimiser lands elsewhere in an
equally good basin, so the fit is not wrong -- it is just not the same numbers
twice, which is enough to make a rebuild undiffable and the backtester's inputs
unreproducible.

These tests pin the mechanism rather than the numerics: the value has to reach
the job's own process, and in the archive rebuild it has to be set before
OpenBLAS loads.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CRONJOB = REPO / "scripts" / "cronjob.sh"
BUILD_SURFACE = REPO / "scripts" / "build_surface.py"

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
    assert _run_under_cronjob()[var] == "1"


def test_cronjob_leaves_a_deliberate_override_alone() -> None:
    """``:=`` not ``=``. The units never set these, so scheduled runs always
    get 1; an operator benchmarking by hand keeps their choice."""
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
