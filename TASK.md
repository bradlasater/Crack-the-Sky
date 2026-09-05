# Stream B — SVI smile fit on top of atm_term_structure (issue #33)

Worktree: `/home/brad-lasater/cts-svi` · Branch: `feat/svi-vol-surface` · Base: `main` (13985c5)

**Draft-PR work brief. Delete this file in the final commit before merge.**

## Step 0 — bootstrap

```
python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt -e .
```

The root checkout's `venv/` is an editable install pointing at `/home/brad-lasater/crack-the-sky`.
Running it from here would silently import the **main checkout's** code and test the wrong tree.

## Context

`atm_term_structure` exists — one row per (date, root, expiry), 2022-08-31 onward — so the term
dimension is built. Nothing exists for the strike dimension: no smile fit, no interpolator, so there
is nothing to evaluate off-strike or between expiries. The surface must be fit over **own** IVs
(European BSM invert in `pricing/iv.py`), never vendor σ.

Scope is **fit + evaluate + tests only**. Calibration consumers (drift_check off-ATM slice) come
later.

## 1. New `pricing/surface.py`

Mirror `pricing/term_structure.py` structurally — it is the correct precedent and it already solved
every upstream problem this needs. Read it first, in full.

**Source the same data.** Read `option_day_bars` via `read_day_bars`, recover contract terms from the
OPRA symbol via `ingest.jobs.parse_option_ticker`, and get the per-expiry forward from
`forward_from_parity`. That buys history back to 2022-08-31 (`option_snapshots` only exists from the
day the live sweep started) and keeps one definition of the forward in the repo. Invert each strike's
own IV with `pricing.iv.implied_vol` in the forward measure (`S=F`, `F=F`, i.e. Black-76), exactly as
`term_structure._invert` does.

**SPX/SPXW only**, per the issue — SPY needs the American IV solver (Stream A / issue #19) before its
strikes are comparable.

Promote `term_structure._bars_to_chain` → `bars_to_chain` (drop the underscore) and import it rather
than copying it. Fit over **OTM strikes only** — calls above `F`, puts below — the standard choice,
and it avoids inverting illiquid deep-ITM day-bar closes.

**Fit.** Raw SVI (Gatheral): `w(k) = a + b(ρ(k−m) + √((k−m)² + σ²))` with `k = ln(K/F)`.
`scipy.optimize.least_squares` over all five params with box bounds (`b ≥ 0`, `|ρ| < 1`, `σ > 0`) and
the domain constraint `a + bσ√(1−ρ²) ≥ 0` for non-negative total variance. Seed **deterministically
from the data** — `a ≈ min w`, `m ≈ ATM k`, `σ ≈ 0.1`, `b` from the wing slopes, `ρ ≈ −0.5` — so a
refit of the same slice reproduces the same params. scipy is already a pinned dependency.

**No new dependencies. No QuantLib** — the issue rules it out explicitly.

**Arbitrage guards**, raising a `SurfaceArbitrageError` (fail loud, matching `marketdata`
conventions):

- butterfly via Gatheral's `g(k) ≥ 0` on a k-grid;
- calendar arb via total variance non-decreasing in T at each k across fitted slices.

**Evaluation API.** A `Surface` holding fitted slices, with `vol(K, T)` interpolating **linearly in
total variance** between bracketing expiries (the arb-preserving choice) and holding flat outside.
This sits *alongside* the existing ATM curve, it does not replace it.

## 2. CLI — but no cron line

`python -m pricing.surface --date … --underlying SPXW` printing fit diagnostics, plus
`scripts/build_surface.py` for the archive, modelled on `scripts/build_term_structure.py`.

**Do not add a scheduled job.** The issue scopes this to fit + evaluate + tests. It also keeps this
PR out of `deploy/crontab` / `deploy/schedule.json` and eliminates the one real conflict with
Stream C (`cts-timers`), which is rewriting the schedule's source of truth.

## 3. Tests — `tests/pricing/test_surface.py`

- Round-trip a synthetic SVI smile within tolerance (the issue's acceptance criterion).
- A synthetic flat Black-76 smile fits near-flat.
- Adversarial butterfly-violating input is detected.
- Calendar-arb input is detected.
- Term interpolation is continuous and hits the fitted slices exactly at their own expiries.
- Too-few-strikes fails loud.

Construct synthetic data the way `tests/pricing/test_from_market_chain.py` does — see
`tests/marketdata/conftest.py` for `snapshot_row` / `forward_row` / `write_records` /
`partition_path`, and note `tests/conftest.py`'s autouse offline guard blocks all outbound sockets.

The issue also wants *"fits a real recent session's SPX chain"* — that needs the box's warehouse and
cannot run in the offline gate (CI has no `DATA_ROOT` and no network). CI gets the synthetic tests;
the real-chain fit is the manual verification step below.

## 4. Docs

Update the vol-surface row in `docs/not-built.html` and add a surface section to `docs/pricing.html`.

**Do not add a new handbook page.** Nav `<ol>` lists are hand-duplicated across all 14 pages with no
include mechanism, and `tests/test_docs_drift.py` checks only that links and anchors *resolve*, not
that the nav lists match — so a new page is 14 manual edits with a silent failure mode. A section on
`pricing.html` is the right size.

## Verify

```
venv/bin/ruff check ingest marketdata pricing tests scripts
venv/bin/python -m compileall -q ingest marketdata pricing
DATA_ROOT=$(mktemp -d) TZ_NAME=America/New_York venv/bin/python -m pytest tests/ -q
```

On the box, against real data:

```
venv/bin/python -m pricing.surface --date <recent session> --underlying SPXW
```

Check the fitted slices are butterfly-clean, that RMS fit error against the own-IV chain is sane, and
that `vol(K,T)` at a fitted expiry's ATM strike agrees with that expiry's `atm_term_structure.atm_iv`.
That last one is a genuine cross-check between the two datasets and is worth writing down in the PR.

## Coordination

- Independent of Stream C (`cts-timers`) — no shared files.
- **Rebase on Stream A** (`cts-amer-iv`) before merging. Both edit `docs/not-built.html` (A touches
  the "Market data and pricing" section, B the "Curves and calendars" section) and
  `docs/pricing.html`.
- Commit prefix convention: `feat:` / `fix:` / `test:` / `docs:`, lower-case imperative.
