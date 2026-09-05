# Stream A — American IV solver, wired into SPY (issue #19)

Worktree: `/home/brad-lasater/cts-amer-iv` · Branch: `feat/american-iv-solver` · Base: `main` (13985c5)

**Draft-PR work brief. Delete this file in the final commit before merge.**

## Step 0 — bootstrap

```
python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt -e .
```

The root checkout's `venv/` is an editable install pointing at `/home/brad-lasater/crack-the-sky`.
Running it from here would silently import the **main checkout's** code and test the wrong tree.

## Context

Issue #19's item 1 (rates curve) is already shipped — `ingest/common/rates.py` has `RateCurve.at()`
doing linear-in-T interpolation, re-exported via `pricing/rates.py`, and PR #34 made `--r` optional
with the curve as default. Items 3 and 4 were split into #33 and #32. **Item 2 is all that's left.**

The gap is load-bearing. `pricing/from_market.py` inverts *every* contract with European BSM, then
prices and Greeks SPY rows with `AmericanCRR`. The canary's `reprice` identity (`market_price` vs
`own_price`, `pricing/drift_check.py:401`) therefore carries the early-exercise premium as permanent
residual on exactly the rows it most wants to check. Both `docs/not-built.html` and
`docs/pricing.html` state that no American IV solver exists.

## 1. `pricing/iv.py`

Add `implied_vol_american(market_price, S, K, T, r, call_put, *, q=None, n_steps=...)` beside
`implied_vol`, following its conventions exactly: fail loud with `ValueError`, never return NaN,
return `0.0` at the intrinsic floor.

Two things differ from the European path and both matter:

1. **Bounds.** `discounted_bounds()` (line 24) is wrong for American. Early exercise makes the lower
   bound *undiscounted* intrinsic — `max(S − K, 0)` for a call, `max(K − S, 0)` for a put — and the
   put's upper bound is `K`, not `Ke^{−rT}`. Add an `american_bounds()` alongside it rather than
   overloading `discounted_bounds` with a style flag.
2. **Brent only, no Newton.** The Newton seed loop (line 98) needs closed-form vega; the CRR tree has
   none, and `engine._bump_greeks`'s central-difference vega costs two extra trees per step. Go
   straight to `brentq` over `[_VOL_LO, _VOL_HI]` with the same bracket-expansion fallback. Use
   `xtol` on σ (~1e-6), **not** the European path's `1e-12` price tolerance — σ accuracy is bounded
   by the tree's own discretization error at `n_steps`, and anything tighter is false precision.
   Say so in the docstring.

`crr_price` is monotone increasing in σ, so the bracketed solve is well-posed.

## 2. `pricing/from_market.py`

**Route the inverter off the already-resolved engine object, not off `contract.exercise_style`.**
This is the subtle part. `_chain_engine` (line 438) downgrades SPY rows to `EuropeanBSM` when the
strike falls outside `--spy-atm-pct`, and `greeks_asof` (line 583) downgrades again once
`--max-rows` American rows are used up. Keying off `exercise_style` would invert those rows with CRR
and reprice them with BSM — breaking the very identity this change exists to fix. `eng` is resolved
at line 577, before the invert at line 593, so it is already in hand.

- `implied_vol_quote()` gains an `engine=` parameter, mirroring `price_quote`/`greeks_quote`.
- Add an `iv_engine` column to `CHAIN_SCHEMA` next to the existing `greeks_engine`, so a row says
  which inverter produced its σ.
- Add a `--euro-iv` escape hatch to force the old behavior for A/B comparison against the canary's
  current thresholds.

### Fold in the curve-caching fix

`resolve_r` (line 207) calls `rate_for` → `load_curve` **once per quote**, and `load_curve`
(`ingest/common/rates.py:103`) scans every `treasury_yields` partition back to 1962 on each call —
it must, because `dt=` there is the ingestion run date, not the curve date. With `--r` now
defaulting to the curve, the canary re-reads the whole rates warehouse hundreds of times per run.
`term_structure.build_for_date` was explicitly fixed for exactly this; its docstring records
~63 min → a few.

Add an `lru_cache` to a private `_load_curve_cached` in `ingest/common/rates.py`, keyed by
`(want, str(data_root))`. It fixes every caller at once and every consumer is a short-lived job.
Note the staleness caveat in a comment. This belongs in *this* PR because American inversion
multiplies the per-row cost; it is also an open IMPROVEMENTS.md item.

## 3. `pricing/drift_check.py`

The change lands on `reprice`. Today a SPY ATM row is inverted with BSM and repriced with CRR, so
its residual *is* the early-exercise premium. After the change the round-trip closes by construction
and `median_reprice` should drop sharply.

`_is_european_row` (line 248) already fences the pair identities (PCP, gamma/vega equality) off from
American rows — that stays correct and unchanged.

**Do not re-tune `--reprice-abs` / `--reprice-rel` in this PR.** Land the solver, read one real run's
`median_reprice` off the box, tighten in a follow-up. Tightening on a predicted improvement is how a
canary gets a threshold it can't hold.

## 4. Tests — `tests/pricing/test_iv_american.py`

Follow `tests/pricing/test_iv.py`'s round-trip + fail-loud style (module docstring stating scope,
`pytest.approx`, `pytest.raises(ValueError, match=...)`).

- `crr_price(american=True)` → `implied_vol_american` recovers σ across a moneyness/DTE grid.
- **American call with q=0 ≡ European IV** — never exercised early. Sharpest available cross-check,
  and it reuses the invariant `tests/pricing/test_american.py` already asserts on prices.
- **An American put price inverted with the European formula overstates σ**; the new solver returns
  strictly less. This is the bug, stated as a test.
- Convergence in `n_steps`: σ at 401 vs 801 agrees to the documented tolerance.
- Fail-loud: put price above `K`, price below intrinsic, non-finite inputs — all `ValueError`.

Extend `tests/pricing/test_from_market_chain.py` with a SPY row asserting `iv_engine` matches
`greeks_engine`, including a strike deliberately outside `--spy-atm-pct` so the downgrade path is
covered.

### On the issue's "reference-check against py_vollib / lets-be-rational"

Both are **European** (`lets_be_rational` is Jäckel's European inversion), so neither is an American
reference, and adding a dependency cuts against this repo's discipline — `requirements.txt` is six
pinned packages and `docs/not-built.html` treats "no pandas" as a stated position. The round-trip,
the q=0 identity, the step-convergence check, and the existing Haug published-price tables in
`tests/pricing/test_haug.py` give a dependency-free reference of equal strength. **This is a
deliberate deviation from the issue text — call it out in the PR body.**

## 5. Docs

- `docs/pricing.html` — the "IV invert" `<dl>` says *"European BSM only — there is no American IV
  solver"*.
- `docs/not-built.html` — move the American IV solver row out of not-built.
- `docs/canary.html` if the reprice wording changes.

## Verify

```
venv/bin/ruff check ingest marketdata pricing tests scripts
venv/bin/python -m compileall -q ingest marketdata pricing
DATA_ROOT=$(mktemp -d) TZ_NAME=America/New_York venv/bin/python -m pytest tests/ -q
```

On the box, against real data:

```
venv/bin/python -m pricing.drift_check --date <recent> --dry-run
venv/bin/python -m pricing.drift_check --date <recent> --dry-run --euro-iv
```

Compare `median_reprice` and the per-identity beyond counts between the two. Confirm SPY rows'
`iv_engine` tracks `greeks_engine` including downgraded strikes. Time the run before and after the
curve-cache fix.

## Coordination

- Independent of Stream C (`cts-timers`) — no shared files.
- **Land before Stream B** (`cts-svi`). Both edit `docs/not-built.html` (different sections) and
  `docs/pricing.html`; B rebases on A.
- Commit prefix convention: `feat:` / `fix:` / `test:` / `docs:`, lower-case imperative.
