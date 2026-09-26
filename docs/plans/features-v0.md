# Plan — feature generation v0 (`vol_features`)

Build plan for `PLAN.md` Week 2 item 5. Written 2026-09-26 against `main` at
`0acc0aa`, the day `rv_forecast` went onto its timer (PR #86). Scope: a new
derived dataset, the job that writes it, its rebuild script, its audit check,
and the docs that describe it. Nothing in `ingest/` capture changes.

Status: **review questions 1–3 resolved 2026-09-26, all as recommended.
Step 0 done. Steps 1–9 not started.**

Three decisions were agreed before this was drafted:

1. **Constant-maturity grid.** Features sit at fixed tenors that match
   `rv_forecast`'s horizons (3/5/10/21/32 sessions), not at listed expiries.
2. **The SPY/SPX mismatch is stated, not hidden.** The forecast is realised
   vol of SPY; the surface is SPXW. Close enough for v0, and written down as
   an assumption (see "Known biases").
3. **v0 features.** `PLAN.md`'s list: realised vol, ATM level and slope, SVI
   skew and curvature, and the variance risk premium. Every value must be
   computable from data available that morning.

---

## What the inputs already guarantee

These were checked against the production archive while drafting, because
the design leans on each of them.

**The grid lines up with no conversion.** On the day-bar path, vol time is
`bus/252`: `T = sessions in (d, expiry] / 252` (`pricing.daycount.TradingSessions`).
An expiry exactly `h` sessions after `d` therefore has `T = h/252`, and
`rv_forecast`'s horizon `h` is the mean daily RV over sessions `d+1 .. d+h`.
Same sessions, same 252. Tenor `h` is `T = h/252`, full stop.

**The book is entirely on business-day time.** In `vol_surface` for
2026-09-24, every expiry out to 330 DTE stamps `bus/252`; only 358 DTE and
beyond falls back to `act/365` (the hybrid convention past the holiday
horizon). The longest tenor here is 32 sessions (~45 calendar days).

**The SPXW surface always brackets the grid.** Across all 673 `vol_surface`
sessions (2022-09-06 to 2026-09-24), every tenor falls between the SPXW
surface's shortest and longest fitted expiry — no session would need
extrapolation. Tenors 3, 5 and 10 land exactly on a fitted expiry in every
session (SPXW lists daily expiries); 21 does in 662 of 673; 32 does in only
180, because daily listings stop short of six weeks, so the long tenor is
usually interpolated.

**The forecast history starts later than the surface.** `rv_forecast` begins
2023-01-05, and its longer horizons only start once they have 63 training rows:
all five horizons are present from 2023-02-16. `spy_spot` begins 2022-08-31.

---

## The dataset

`clean/vol_features`, one row per **(date, horizon)**. `date` is the session
whose close the inputs describe. Five rows per session.

| column | type | meaning |
|---|---|---|
| `date` | string | session `d` (inputs are `d`'s close) |
| `underlying` | string | `SPXW` (the surface root; see step 1) |
| `horizon` | int64 | tenor in sessions: 3 / 5 / 10 / 21 / 32 |
| `t_years` | float64 | `horizon / 252` |
| `daycount` | string | `bus/252`, stamped like every vol-time row |
| `atm_vol` | float64 | ATM-forward implied vol at the tenor |
| `atm_fwd_vol` | float64 | forward ATM vol from the previous tenor to this one |
| `skew` | float64 | dσ/dk at k = 0 |
| `curvature` | float64 | d²σ/dk² at k = 0 |
| `rv_ann_5` | float64 | trailing 5-session realised vol, annualised |
| `rv_ann_22` | float64 | trailing 22-session realised vol, annualised |
| `fc_vol_ann` | float64 | `rv_forecast.vol_ann` for this horizon |
| `fc_log_rv_mean` | float64 | `rv_forecast.log_rv_mean` |
| `fc_log_rv_sd` | float64 | `rv_forecast.log_rv_sd` |
| `vrp_var` | float64 | `atm_vol² − fc_vol_ann²` |
| `vrp_vol` | float64 | `atm_vol − fc_vol_ann` |
| `vrp_z` | float64 | where implied variance sits in the forecast distribution |
| `on_node` | bool | tenor landed exactly on a fitted expiry (no interpolation) |
| `exp_lo` / `exp_hi` | string | the bracketing expiries (equal when `on_node`) |

The realised-vol columns are the same on all five rows of a session. Keeping
them on every row costs a few bytes and saves every consumer a join.

**Every row is complete or absent.** No column above is nullable. A session
where any input is missing gets no row for the affected tenor, rather than a
row full of nulls that a backtest would silently drop or, worse, fill.
Consequence: the archive starts at 2023-02-16, the first session where
all five forecast horizons exist.

---

## The features, precisely

Notation: `k = ln(K/F)`, log-moneyness against the slice's own parity
forward; `w(k, T) = σ²(k, T)·T`, total implied variance; raw SVI per slice,
`w(k) = a + b(ρ(k − m) + √((k − m)² + s²))` (the code calls `s` `sigma`).

### Interpolating the surface to a tenor

For tenor `T`, take the bracketing fitted slices `T_lo ≤ T ≤ T_hi` and
interpolate **linearly in total variance at fixed `k`**:

```
w(k, T) = w_lo(k) + (w_hi(k) − w_lo(k)) · (T − T_lo) / (T_hi − T_lo)
```

This is what `Surface.vol` already does, with one difference that matters
here: `Surface.vol(K, T)` fixes the *strike* and evaluates each slice at its
own forward's `k`, and it holds the nearest slice flat outside the fitted
range. Features need fixed *moneyness* (ATM forward is `k = 0` on every
slice, whatever the forwards) and must **refuse** to extrapolate, not hold
flat. So step 2 adds a small `Surface.total_variance(k, T)` (with derivatives)
rather than reusing `vol`. Linear-in-`w` at fixed `k` is also the
interpolation the calendar guard already protects: `w` is non-decreasing in
`T` at every `k` on the fitted grid, so the interpolated `w` cannot go
negative or produce a negative forward variance.

On a node (`T == T_lo`) the result is that slice bit-for-bit, the same
guarantee `Surface.vol` gives.

### Implied features

- **`atm_vol`** = `√(w(0, T) / T)`.
- **`atm_fwd_vol`** — the slope feature. Forward ATM variance between the
  previous grid tenor `T_p` and this one:
  `√((w(0, T) − w(0, T_p)) / (T − T_p))`, with `T_p = 0` for the first
  tenor (so for h=3 it equals `atm_vol`). Chosen over a finite-difference
  slope of `atm_vol` because it is the quantity a calendar spread actually
  trades, and the calendar guard makes it well-defined (never the square
  root of a negative number). **Review question 1: agreed.**
- **`skew`** = `∂σ/∂k` at `k = 0`. From `σ = √(w/T)`:
  `σ' = w' / (2√(w·T))`, with SVI `w'(k) = b(ρ + (k − m)/√((k − m)² + s²))`,
  interpolated in `T` the same way as `w` (the derivative of a linear
  interpolant is the interpolant of the derivatives).
- **`curvature`** = `∂²σ/∂k²` at `k = 0`:
  `σ'' = w'' / (2√(w·T)) − w'² / (4·√T·w^{3/2})`, with SVI
  `w''(k) = b·s² / ((k − m)² + s²)^{3/2}`.

Skew and curvature are in σ-per-unit-`k`, not per strike point or per delta.
They are the SVI surface read at the money; a 25-delta risk reversal or
butterfly would need a delta mapping and is left for v1.

### Realised features

From `spy_spot` via `signals.har_rv.realized_variances` (so gaps are handled
the same way: a missing session voids the returns around it, and a window
containing a void yields no value):

- **`rv_ann_5`** = `√(252 · mean(rv over sessions d−4 .. d))`
- **`rv_ann_22`** = `√(252 · mean(rv over sessions d−21 .. d))`

Windows match HAR's weekly and monthly legs, so these are the forecast's own
inputs, not a second definition of realised vol.

### Forecast and variance risk premium

`fc_*` are copied from the `rv_forecast` row with origin `d` and the same
horizon. Then:

- **`vrp_var`** = `atm_vol² − fc_vol_ann²`, annualised variance.
- **`vrp_vol`** = `atm_vol − fc_vol_ann`, the same thing in vol points, for
  readability.
- **`vrp_z`** = `(ln(atm_vol² / 252) − fc_log_rv_mean) / fc_log_rv_sd`.
  `atm_vol²/252` is the implied *mean daily* variance, the same quantity the
  forecast's lognormal describes, so this says how many forecast standard
  deviations the market's number sits above the model's median. It uses the
  forecast's full distribution, which is why the forecast stores one.

**Review question 2 (agreed, ATM for v0):** these compare the forecast to *ATM* implied variance.
The textbook variance risk premium uses the variance-swap strike (the
integral over the whole smile, as the VIX does), which sits above ATM when
skew is negative. ATM is the simpler, more robust v0 number; the swap strike
is computable from the SVI slices and is the natural v1 upgrade.

---

## Point-in-time discipline

Every input for session `d` comes from `d`'s close: `vol_surface dt=d` (fit
to `d`'s day bars), `rv_forecast` origin `d`, `spy_spot` up to and including
`d`. None of it exists before the next morning. Day bars arrive with the flat
files at 11:05 ET on `d+1`, and the surface lands ~12:16.

So **a `vol_features` row for session `d` is usable for a decision on `d+1`,
after 12:20 ET, and never for a decision on `d`.** The job enforces its half
by reading only partitions `≤ d`. The backtester has to enforce the other
half by lagging one session; this plan states that rule so the backtester
plan can cite it, and step 6 pins the job's half with a test.

---

## Known biases (stated, not fixed, in v0)

- **SPY realised against SPX implied.** Tracking error between the two is
  small next to the premium being measured, but not zero. SPY also closes at
  16:00 and SPX options at 16:15, so the two closes differ by a quarter hour
  of trading.
- **SPY ex-dividend drops are in the realised vol.** `spy_spot` is the
  unadjusted close, so four times a year the close-to-close return includes a
  ~0.3% drop that is not market movement. The squared drop is roughly a tenth
  of an ordinary day's variance on those four days: small, but it biases the
  forecast up and the premium down. This belongs in `spy_spot` or `har_rv`,
  not here; noted so the backtest attribution knows it is there.
- **ATM, not variance-swap, implied variance** (review question 2).

---

## Where it runs

| | |
|---|---|
| module | `signals/vol_features.py` (job `vol_features`) |
| dataset | `clean/vol_features` |
| timer | `massive-vol-features`, **Tue–Sat 12:20 ET** |
| rebuild | `scripts/build_vol_features.py [--start] [--end] [--force]` |

12:20 sits after `surface` (12:15, which takes ~50 s) and before
`coverage_audit` (12:30). `coverage_audit.PIPELINE_DONE_ET` moves from 12:15
to 12:20, since this job becomes the last link in the chain; the constant's
comment already explains why it exists. Through `scripts/cronjob.sh` like
every job. No BLAS underneath (closed-form SVI evaluation), so no
`blas_threads` stamp, the same reasoning as `atm_term_structure`.

---

## Steps

Each lands with tests; the order is chosen so every step is runnable on its
own.

**Step 0 — housekeeping found while scoping** (separate small commit):
correct the stale status line in `docs/plans/trading-day-calendar.md` (it
still says the `blas_threads` recut is pending; the archive is recut), and
refit 2026-09-14's surface under the pin. That session carries the only 60
unpinned `vol_surface` rows (`scripts/build_surface.py --start 2026-09-14
--end 2026-09-14 --force`, pinned by the script).
**Done 2026-09-26.** The refit reproduced all 300 SVI parameters bit-for-bit;
only the stamp changed. The archive has no unpinned rows left.

**Step 1 — pick the root.** SPXW, because it lists a daily expiry and so
brackets every tenor on a node or close to one. SPX monthlies are AM-settled:
they settle at the expiry day's open, so the session count overstates their
vol time by most of a session, which is a large error at a 3-session tenor. **Review
question 3: agreed, SPXW only for v0.**

**Step 2 — `Surface.total_variance(k, T)` and its k-derivatives** in
`pricing/surface.py`: linear in `w` at fixed `k`, exact on a node, raising
(not holding flat) outside `[T_first, T_last]`. Tests: exact on a node,
linear between nodes on a synthetic two-slice surface, refuses both ends,
derivatives agree with finite differences.

**Step 3 — schema** for `vol_features` in `ingest/schemas`, and the
non-null contract in `marketdata/validate.py` (every column required).

**Step 4 — `signals/vol_features.py`**: `build_for_date(settings, d)`
reads `vol_surface`, `rv_forecast` and `spy_spot` at or before `d` and
returns the rows; `main` defaults `--date` to the previous trading day, as
`har_rv` does. A missing input for a tenor means no row for that tenor, and
a session with **no** rows raises (the job fails loudly rather than landing
an empty partition).

**Step 5 — rebuild script** `scripts/build_vol_features.py`, the same shape
as `build_rv_forecast.py`: load inputs once, write per date, skip existing
unless `--force`.

**Step 6 — tests**:
- no lookahead: landing partitions after `d` does not change `d`'s rows
  (the `har_rv` truncation test, applied here);
- a flat synthetic surface (constant σ, zero skew) gives `atm_vol = σ`,
  `atm_fwd_vol = σ`, `skew = 0`, `curvature = 0`;
- a constant spot-vol series gives `rv_ann_*` equal to that vol, and with a
  matching forecast `vrp_var = 0`;
- a missing input drops exactly the affected rows; no rows at all raises;
- the interpolated tenor lies between its brackets and marks `on_node=false`.

**Step 7 — schedule and monitoring**: the `deploy/schedule.json` entry and
healthcheck; extend `test_rv_forecast_runs_between_its_input_and_its_audit`
into a chain test (`spy_spot` < `rv_forecast` < `surface` < `vol_features` <
`coverage_audit`); move `PIPELINE_DONE_ET`; add a `vol_features` audit check
(FAIL on a missing partition or tenor), mirroring `rv_forecast`'s.

**Step 8 — docs**: `docs/ingest.html` schedule row, `docs/data-flow.html`
derived-dataset entry, `PLAN.md` item 5, and this file's status line.

**Step 9 — deploy** (after merge, same as #86): playbook, Healthchecks
setup, `build_vol_features.py` over the archive, then a manual `box` run.

---

## Review questions — resolved 2026-09-26

1. **Slope as forward vol (`atm_fwd_vol`)**, rather than a finite-difference
   slope of ATM vol between tenors. **Yes.**
2. **ATM implied variance for the premium in v0**, with the variance-swap
   strike deferred to v1. **Yes.**
3. **SPXW only**, with SPX left out of v0. **Yes.**

## Not in scope

- Regime flags and event calendars (FOMC, CPI, OPEX). They are features, but
  they need their own data source.
- Snapshot-based (intraday) features. The live path is still ACT/365 until
  the calendar plan's step 4, which is gated on its open decision 2.
- Anything the strategy engine does with these rows (`PLAN.md` item 6).
