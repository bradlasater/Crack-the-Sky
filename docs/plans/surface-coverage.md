# Plan — close the `vol_surface` coverage gap

Written 2026-09-26 against `main` at `0acc0aa`. Found while building
`vol_features` (`docs/plans/features-v0.md`, "Blocked on surface
coverage"), which cannot run over history until this is fixed. Scope:
`pricing/surface.py`, its tests, `coverage_audit`'s `vol_surface` check,
a rebuild of `clean/vol_surface`, and the docs that describe the surface.

Status: **decisions 1–6 resolved 2026-09-26, 8 on 2026-09-27; decision 7
reopened pending H's measurement. Steps 1–3c done, 3c measured;
finding 7 corrected and finding 8 added 2026-09-27. Next: step 4 (C).**

---

## The gap

`vol_surface` has **673 of 1,020 sessions**. Every input it is fit from
(`option_day_bars`, and the `atm_term_structure` and `spy_spot` built beside
it) has all 1,020. The 347 missing sessions are scattered across every year
from 2022 to August 2026, and none of the docs record the gap. The only trace
is "27 of the 660 sessions that fit cleanly" in the BLAS-pin measurement,
which states a count without saying it is two-thirds of the history.

The gap is not random. Ranking sessions by trailing 22-session SPY realised
vol, 29% of the calmest quartile is missing against 38% of each of the two
upper quartiles. Any backtest on the surface, or on the features built from
it, under-samples the sessions where a volatility strategy makes or loses the
most.

## What the diagnostic found

Every one of the 347 sessions was refit in memory, each root separately,
under the production thread pin (8), with nothing written. The script is
`scripts/diagnose_surface_coverage.py`; its per-session output is not
committed, since rerunning the script reproduces it.

**1. One root's failure discards the other.** `build_surfaces` fits the roots
in a loop, and any exception escapes the loop, so nothing is written for the
session. In **184 of the 347 sessions SPXW fits cleanly on its own**, and was
lost only because SPX failed. SPXW is the root the book and the features use.

| outcome, by session | sessions |
|---|---|
| SPX fails, SPXW fits | 184 |
| SPX fits, SPXW fails | 122 |
| both fail | 41 |

Per root, over the 347: SPX fails in 225 (134 calendar, 69 butterfly, 22 did
not converge); SPXW fails in 163 (56 calendar, 95 butterfly, 12 did not
converge).

**2. The two roots fail in different places.** *Corrected by step 1.* The
first version of this finding said nearly every failure was in a long-dated
expiry. That is true of SPX and false of SPXW. The step 1 rerun names the
failing expiry for every failure:

| root, failure | 1–7 | 8–45 | 46–180 | 181–400 | >400 DTE |
|---|---|---|---|---|---|
| SPX butterfly | 0 | 5 | 14 | 23 | 27 |
| SPX calendar (incl. floor) | 1 | 9 | 12 | 49 | 63 |
| SPX did not converge | 0 | 0 | 0 | 2 | 20 |
| SPXW butterfly | 27 | 62 | 4 | 2 | 0 |
| SPXW calendar | 17 | 34 | 4 | 1 | 0 |
| SPXW did not converge | 7 | 3 | 1 | 1 | 0 |

SPX fails in its LEAPS. **SPXW fails inside the book: 150 of its 163
failures are expiries of 45 days or less.** The earlier reading came from
the moneyness range printed in the butterfly error, which is the *padded*
check grid (finding 6), not the strikes the expiry was fit to.

Calendar rejections still sit deep in the wings for both roots: 97% at
`|k| > 0.5`, and the 51 SPXW ones in the book have a median `|k|` near 1.5,
far outside any short expiry's strikes (finding 3).

**3. The calendar check runs where no expiry has data.** Both the Surface
guard and the fit-time calendar floor evaluate on the session's *union*
grid, `[min k_min, max k_max]` over every expiry. That grid is set by the
LEAPS: its left edge is around −1.1 to −1.7 in recent months. A pair of
expiries is therefore compared far outside the strikes either was fit to.
1–7 DTE expiries reach a median `k_min` of −0.15, so for them almost the whole
grid is extrapolated SVI wing. No-arbitrage in total variance is a statement
about prices; the guard is enforcing it on a region no price was observed in,
and a slice's extrapolated wing is then asked to act as a hard floor for the
next one.

**4. September is not protected by anything the rebuild lacks.** At
August's rate (6 of 21 sessions missing), the 17 September sessions the
daily job has fit would lose about five, and they lost none (under a 1%
chance if nothing differs). The candidate was that the daily job and a
rebuild see different calendars. Step 1 refit all 17 September sessions
today, under today's calendar and code: **every one still fits**, for both
roots, and the six missing August sessions still fail. So the difference is
in the sessions themselves, not in how they were built, and nothing about
coverage depends on rebuild-versus-daily. Why September fits so cleanly is
left open; the fix does not need it.

**5. A rebuild reads a rate curve the daily job could not have seen.** The
same refit found that 27 of the 34 September (session, root) fits do **not**
reproduce the landed parameters, though the fit is deterministic. T and the
day-count stamp are identical on every slice; the discount rate is not
(3.81% landed against 3.91% refit, for 2026-09-10). `load_curve(d)` returns
the newest curve *dated* on or before `d`, scanning every partition. The
Treasury curve publishes two days late (the `dt=2026-09-10` partition holds
the curve dated 09-08), so:

- the daily job, fitting session `d` at 12:15 on `d+1`, sees curves dated up
  to `d−1`;
- a rebuild, run any time after `d+2`, finds the curve dated `d` itself.

That is a one-day look-ahead in every rebuild of every derived dataset that
prices off the curve: `vol_surface`, `atm_term_structure`, and anything
through `rate_for`. The live snapshot path runs on the day and is not
affected. The numbers barely move, because the rate enters the fit only
through the parity forward's discount factor, and a 10bp difference over a
few weeks is a relative change of about 1e-4. But it means the archive is
two things: the 660 sessions from the 09-08 rebuild used next-day curves,
the daily sessions since did not, and no rebuild can reproduce a daily
partition. A backtest's inputs would then quietly include a curve published
after the decision it informs. **Decision 5.**

**6. The butterfly check also runs far beyond the data.** It evaluates
Gatheral's g(k) on the fitted strikes padded by `G_PAD = 1.0` in `k` on each
side, a constant set in the first surface commit with no stated basis. For a
3-day SPXW expiry, whose strikes span roughly `k ∈ [−0.15, 0.05]` and whose
one-standard-deviation move is about 0.02 in `k`, that is a check out to
strikes at 32% and 286% of the forward, dozens of standard deviations past
any price. Recomputing the worst g(k) for each of the 95 SPXW butterfly
failures, from the parameters in its error:

| grid | pass |
|---|---|
| fitted strikes only | 83 / 95 |
| strikes ± 3 sd of the slice (`3·√w_min`) | 81 / 95 |
| strikes ± 0.25 | 50 / 95 |
| strikes ± 0.5 | 32 / 95 |
| strikes ± 1.0 (today) | 0 / 95 |

So 81 of the 95 are arbitrage-free wherever the smile is within three of its
own standard deviations of a quoted strike, and fail only in the
extrapolation. The remaining 12–14 have genuine arbitrage inside or near the
data, which is what dropping a slice (C) is for. These use the failing fit's
parameters, so a refit under a narrower grid may land slightly elsewhere;
step 6 measures the real outcome. 37 of the 95 also break Lee's asymptotic
wing-slope bound, `b(1 + |ρ|) ≤ 2`, which only binds infinitely far out.

**7. F's regressions are fits the old check should never have passed.**
*Found measuring step 3b; corrected 2026-09-27.* With B and F together,
SPXW fits in 280 of the 347 missing sessions (93.4% coverage before C), but
6 of the 30 previously good sessions in the check sample now fail SPXW (and
3 SPX), all on calendar crossings inside the data at `k ≈ 0.06–0.24`. Under
B alone all 30 fit. Traced on 2022-11-29, expiry 2022-12-07, fit under its
real calendar floor:

| | fit kept | a | b | ρ | σ | rms | min g, exact |
|---|---|---|---|---|---|---|---|
| unconstrained | — (goes to repair) | 0.0004 | 0.022 | −0.999 | 0.061 | 4.3e-5 | −0.010 |
| B alone | the repair's refit | 0.0004 | 0.021 | −0.999 | 0.059 | 4.4e-5 | −0.0004 |
| B + F | the repair's refit | −0.750 | 0.392 | −0.162 | 1.942 | 1.04e-4 | ≥ 0 |

The first version of this finding read the table the other way: that
`least_squares` stopped in a poor basin that F's narrower check let through,
and that the wide pad had rescued the better one. Tracing the fit showed
the opposite. The unconstrained fit is in the low-rms basin and violates
the butterfly condition under either pad, so both builds repair it. Under
B alone the repair kept a fit in that basin that still prices a negative
density, in a band at `k ≈ −0.176` to `−0.166` straddling the last quoted
put. The flat pad's 201 points sat 0.011 apart and stepped over it (finding
8). F's grid is five times denser over this slice, rejects that fit, and the
best feasible refit is the wide parabola. Its right edge sits too high for
the next expiry to clear inside the data, so the session fails at 12-08.

So G, which proposed keeping the best of the two candidates, would not
help: here both SLSQP starts are projected to the same flat anchor (the
calendar floor, peaking at 0.0038, admits no nearer point on either
segment), and both land in the parabola. Decision 7 waits on the
measurement after H.

**8. The butterfly check misses bands of negative density between its grid
points.** *Found 2026-09-27, tracing finding 7.* The check evaluated g(k)
at a fixed 201 points, whatever the range. Across the landed archive, on a
reference grid at 2e-5 spacing:

| | landed slices |
|---|---|
| all | 37,815 |
| negative density inside their own strikes | **675** (607 SPXW), in 331 sessions |
| of which inside the book (≤ 45 DTE) | 633 |
| negative density within the old 1.0 pad | 22,936 |

The worst are 2-day SPXW expiries, down to g = −0.0086 inside the data. A
denser lattice alone does not close it: over each slice's F range, a 0.0025
lattice still misses 97 of the 3,722 slices that violate there, 0.001
misses 22, and 0.0005 misses 8. The last misses are near-tangent dips about
0.001 wide (median g ≈ −5e-7), not tied to σ, so no fixed spacing is safe.
Minimising g between the neighbours of each grid-local minimum finds all
3,722 at the 0.0025 lattice, at 1.6 local minima per slice on average and
3 at most.

---|---|---|---|---|---|---|
| B alone | the repair's refit | 0.0004 | 0.021 | −0.999 | 0.059 | 4.4e-5 |
| B + F | the first fit | −0.750 | 0.392 | −0.162 | 1.942 | 1.04e-4 |

`least_squares` from the one data-derived seed stopped in a basin more than
twice as bad: a wide parabola with a large negative `a`. Under the flat
1.0 pad that fit failed the butterfly check, so it went to the repair, which
restarts from a projected seed and found the better basin. F's narrower
check accepts the poor fit as it is. Its right edge then sits too high for
the next expiry to clear inside the data, and the calendar guard rejects the
session. The wide pad was a lucky rescue, not a design. Lee's bound would
not catch this one (its wing slope is 0.46). This is also the likeliest
source of the odd shapes F lets through (34 of its 81 recovered slices
break Lee's bound).

---

## The fix

In order of what they recover and how contained they are.

**A. Fit each root on its own.** A root that fails is logged and skipped;
the others still land. Recovers the 184 sessions outright, with no change to
any fitted number. `coverage_audit`'s `vol_surface` check keeps FAILing on a
missing SPXW (the book's root) and **WARNs** on a missing SPX, since nothing
downstream needs SPX today. **Decision 1.**

**B. Check calendar arbitrage where both expiries have data.** For each
adjacent pair, both the Surface guard and the fit-time floor evaluate on the
*overlap* of the two fitted ranges, `[max(k_min), min(k_max)]`, not the
union grid. That removes the comparisons in extrapolated wings (the 97% at
`|k| > 0.5` are the target) and the floors that have no feasible seed. The
cost, stated plainly: the surface no longer promises calendar-arbitrage-free
wings *beyond the data*. Every consumer today reads inside the data. The
features read `k = 0`, and drift_check's off-ATM canary reads quoted strikes.
The docstrings will say so, and `Surface.vol`/`total_variance` keep raising
or holding flat exactly as now. **Decision 2.**

**C. Drop a slice that cannot be fit, not the session.** A slice that fails
to converge or fails the butterfly guard even after repair is dropped and
logged, and the calendar chain continues from the previous kept slice. A
session still fails if it keeps no slice. Every kept slice passes the same
guards as now, so nothing weaker lands. The dropped count is logged per
session, and `coverage_audit` WARNs when a session drops a slice inside the
book (≤ 45 DTE), since a hole there would move a feature's interpolation
bracket. **Decision 3:** log-only, or a per-session record in the dataset.

**D. Stop fitting expiries nobody reads (optional).** Fitting only up to a
DTE cap (e.g. 400 days) would remove most failing slices at the source and
make the fit faster. It also narrows the union grid for any consumer still
reading it. With B and C in place it is not needed for coverage, so it is a
scope decision, not a fix. **Decision 4.**

**E. Read the curve as the daily job would have (finding 5).** Give
`load_curve` an availability cut-off: for session `d`, only partitions
landed by the morning of the next session (`dt ≤ d+1`, since each
partition lands at 08:20). Every derived builder passes it, and the daily
job's behaviour is unchanged by construction. This is not coverage, but it
has to land before step 7's rebuild, or the rebuilt archive carries the
look-ahead forward. **Decision 5.**

**F. Check the butterfly condition near the data, on the slice's own scale
(finding 6).** Replace the flat `G_PAD = 1.0` with a pad of three of the
slice's standard deviations, `3·√w_min` where `w_min` is the SVI minimum
total variance, used by both the guard and the constrained refit. A 3-day
expiry is then checked about 0.06 past its strikes, a two-year one at 18%
vol about 0.75, close to today's pad. The cost is the same kind as B's: no butterfly promise
in the far extrapolated wing of a short expiry. Lee's wing-slope bound stays
a logged diagnostic, not a rejection. **Decision 6.**

**G. Keep the best fit, not the first feasible one (finding 7).** Always
run both starting points the repair already uses (the unconstrained optimum
and the projected data seed) and keep whichever feasible candidate has the
lower RMS, rather than accepting the unconstrained fit whenever it passes
the guards. Deterministic, since the seeds are. The cost is an SLSQP solve
per slice instead of only per violating slice, so fits get slower; step 6
measures by how much. It changes the parameters of any slice where the
repair's basin is better, including slices in sessions that fit today, so
it lands before the rebuild like everything else here. **Decision 7.** *Its premise did not survive tracing (finding 7,
corrected): it does not recover the traced session. Built but not committed;
kept or dropped on the step 6 numbers after H.*

**H. Make the butterfly check exact over its range (finding 8).** Evaluate
g on the `K_STEP` lattice across the padded range, plus both ends, instead
of a fixed 201 points, and minimise g between the neighbours of every local
minimum on it. The guard, the feasibility checks and the repair's seed
projection all use that minimum. The constrained refit still constrains g
at the lattice points, with its margin; a refit that dips below zero
between them is infeasible and is not kept. This tightens the check rather
than loosening it, so it will cost coverage before C recovers it, and it
changes fits in sessions that land today, so it lands before the rebuild.
**Decision 8.**

What is *not* proposed: loosening the butterfly tolerance (`G_TOL`),
raising the arbitrage repair budget, or holding a failed slice's parameters
from a neighbour. B and F do change what the checks promise: both stop
enforcing no-arbitrage in extrapolated wings no price was observed in. Every
slice that lands still passes both checks everywhere near its data.

---

## Steps

**Step 1 — measure, and settle the September question.** Commit the
diagnostic as `scripts/diagnose_surface_coverage.py` (in-memory refit per
root, per-expiry failure record, nothing written). Extend it to name the
failing expiry and its DTE for every failure. Then test the calendar
hypothesis in finding 4: refit a sample of the missing August sessions with
the holiday horizon as it stood the next morning. Record the result here
before any fix lands, because it decides whether the rebuild also needs a
point-in-time calendar.

**Step 1 results (2026-09-26). Done.** The diagnostic is committed. The
September refit settled finding 4 and surfaced finding 5; the per-expiry
rerun corrected finding 2 and surfaced finding 6.

**Step 2 — A: independent roots.** `build_surfaces` catches a root's failure,
logs it and continues; `build_for_date` raises only when no root fits.
`coverage_audit`: FAIL on missing SPXW, WARN on missing SPX. Tests: one
root raising still lands the other; no root raises.
**Done 2026-09-26.** `pricing.surface.build_session` reads the day bars and
curve once and fits each root on its own, returning what landed and why the
rest did not. The scheduled job and `scripts/build_surface.py` go through it;
`build_for_date` stays strict for the diagnostic. On 2022-09-01, a lost
session, SPXW now lands 28 slices while SPX's butterfly failure is reported
beside it. Nothing changes for a session where both roots already fit.

**Step 3 — B: overlap-grid calendar check** in `Surface._check_calendar` and
the fit-time floor. Tests: a pair crossing only outside their overlap is
accepted; a crossing inside the overlap is still rejected (the existing
calendar tests must still pass unchanged); a disjoint pair (no overlap) is
accepted with nothing to compare, and says so in the log.

**Step 3 as built (2026-09-26): two changes from the text above.**

- **Every pair, not only neighbours.** On the union grid, checking adjacent
  pairs was enough: every pair shared one grid, so `w₁ ≤ w₂ ≤ w₃` at each
  point carried through. On overlaps it does not: a strike covered by
  expiries 1 and 3 but not 2 would never be compared. The guard compares
  every pair on its overlap, and the fit-time floor is the highest earlier
  slice wherever that slice has data.
- **One shared lattice.** The first cut sampled the floor on the new slice's
  own range and the guard on each pair's overlap, two different point sets.
  Loading the 673 landed sessions under it rejected 1,106 of 1,346
  root-surfaces, all on hairline crossings (1e-7 to 6e-6 in total variance)
  between one grid's points that the other never constrained. The old
  design had avoided this by using one grid for both. The fix keeps that
  property: floor and guard both evaluate only at multiples of
  `K_STEP = 0.0025` in `k` inside the relevant range, so a slice fit to
  clear the floor passes the guard at the same points.

**Step 3 measured (2026-09-26).** Refit under the new code, in memory:

- *30 randomly chosen landed sessions:* all 30 still fit, both roots.
- *The 181 sessions with a calendar rejection:* SPXW now fits in 173 (110
  before). By root: SPXW's 56 calendar failures leave 5; SPX's 134 (129
  calendar, 5 floor) leave 6. Eighteen butterfly failures also clear,
  because the refits land differently once the wing floor is gone.
- *Remaining calendar failures (12, 5 of them SPXW)* are mostly inside the
  data, e.g. 2022-09-21 SPX at 30 DTE drops 0.076 at `k = 0.015`. Those are
  genuine crossings for C to drop.
- *Two regressions, both SPX:* sessions that fit before and now fail the
  all-pairs check, which finds crossings between non-adjacent expiries the
  old adjacent check could not see. A missing SPX is a WARN.

**Consequence for deploying.** The landed archive was fit against the old
union grid's points, so the new guard rejects most of it on load (1,134 of
1,346 root-surfaces), and anything that reads the surface would fail on
old partitions. The code and the rebuilt archive therefore have to go live
together, through step 7's staged rebuild and swap, the same constraint the
`daycount` and `blas_threads` changes had. Rollback likewise means both.

**Step 3b — F: slice-scaled butterfly pad** in `_butterfly_grid`
and its callers. Tests: a short slice with arbitrage only past three
standard deviations of its strikes is accepted; arbitrage inside that range
is still rejected, and still repaired where the repair holds; a long slice's
pad stays near today's.

**Step 3c — H: exact butterfly check** in `_butterfly_grid`, `_min_g` and
`_check_butterfly`. Tests: the grid is the lattice plus its ends; the
2022-12-07 fit that stepped over the old grid is rejected; a real
near-tangent dip that the grid's own points miss is rejected; on a clean
smile the refined minimum matches a dense grid.
**Built 2026-09-27.**

**Step 3c measured (2026-09-27).** B + F + H, refit in memory:

| | SPXW fits | SPX fits |
|---|---|---|
| 347 missing sessions, B + F | 280 | 309 |
| 347 missing sessions, B + F + H | **204** (82 lost, 6 gained) | 295 (17 lost, 3 gained) |
| 30 landed sessions, B + F + H | 21 | 28 |

SPXW's 143 failures by cause and the failing expiry's DTE. The diagnostic
records only the first failing expiry of each session, so a session may
hold more:

| | ≤ 5 DTE | 6–45 DTE | > 45 DTE |
|---|---|---|---|
| butterfly (90) | 67 | 23 | 0 |
| calendar or no convergence (53) | 10 | 31 | 12 |

So H costs what the check had been missing: the lost sessions are mostly
fits that passed only between grid points, three-quarters of them expiries
five days or less out. They are single slices, which C drops. What C leaves
is holes at the short end of the book, and the features must interpolate
across them or report the tenor missing. 2022-11-29, finding 7's session,
now fits (34 slices). Fits take about 20% longer per session.

**Step 4 — C: drop-a-slice.** Tests: a non-converging middle slice is
dropped and the chain floors off its predecessor; a session whose every
slice fails still raises; dropped book-tenor slices surface in the audit.

**Step 5 — D, if chosen.**

**Step 5b — E:** the curve cut-off in `ingest/common/rates.py`,
passed by `pricing.surface`, `pricing.term_structure` and `rate_for`'s
derived callers. Tests: a curve landed after `d+1` is invisible to `d`; the
daily path picks the same curve it picks today. `atm_term_structure` and
`spy_spot` then need the same staged rebuild as the surface.

**Step 6 — measure again.** Rerun the step 1 diagnostic over **all 1,020
sessions** under the new code. Acceptance: SPXW lands in at least 99% of
sessions; no book-tenor (≤ 45 DTE) slice changes for a session that already
landed, beyond what B changes by design (reported, not assumed); and the
volatility-quartile skew in the missing sessions is gone. If SPXW falls
short, stop and report rather than loosen a guard.

**Step 7 — rebuild and swap.** Every session's chain can change under B, so
this is a full rebuild. It follows the calendar plan's decision-5 procedure:
rebuild into staging, verify every partition, swap outside the Tue–Sat
12:00–12:30 window with `surface`/`coverage_audit` stopped, and keep the old
tree until a week of jobs is clean. Nothing else has to be rebuilt:
`vol_features` has not landed yet, and drift_check reads only T-1.

**Step 8 — docs.** `docs/pricing.html` (the guard's grid, the drop rule, the
per-root rule), `docs/ingest.html` (the audit check), IMPROVEMENTS.md (the
gap as a found-and-fixed entry), and `features-v0.md`'s blocked section.

---

## Decisions — resolved 2026-09-26 and 2026-09-27

1. **Per-root landing.** Land whichever roots fit; FAIL on missing SPXW,
   WARN on missing SPX? **Yes.**
2. **Overlap grid.** Check calendar arbitrage only where both expiries of a
   pair have data, accepting that the extrapolated wings carry no calendar
   guarantee? **Yes.**
3. **Dropped slices.** Log them only, or also record a per-session count in
   the dataset? **Log plus the audit WARN**; a column would be a schema
   change for a diagnostic.
4. **DTE cap.** Stop fitting past ~400 DTE? **Not now**; decide after step 6
   shows what B and C leave.
5. **Point-in-time curve.** Fix the one-day curve look-ahead in this plan,
   before the rebuild? **Yes, as E** (resolved 2026-09-26). The surface
   rebuild is happening anyway, and doing it without E would bake the
   look-ahead into 1,020 fresh partitions.
6. **Slice-scaled butterfly pad.** Check g(k) out to three of the
   slice's standard deviations past its strikes, instead of a flat 1.0 in
   `k`? **Yes, as F** (resolved 2026-09-26). It recovers about 81 of the 95
   SPXW butterfly failures, most of them inside the book, which C would
   otherwise drop and leave as holes in the features' grid.
7. **Best-of-candidates fit (reopened).** Keep the lowest-RMS feasible fit across
   both starting points, rather than the first fit that passes (G)?
   *(Recommended: yes. Without it, F trades one kind of failure for another
   and keeps poor fits, and the alternative is reverting F and letting C drop
   the 95 SPXW slices, most of them inside the book.)*
   *(2026-09-27: resolved yes, then reopened. Tracing the case it was for
   showed the better fit prices a negative density (finding 7, corrected),
   so G does not recover it. Revisit on step 6's numbers after H.)*
8. **Exact butterfly check.** Check g between grid points, not only at them
   (H), accepting that fits which only passed by aliasing will now fail and
   fall to C? **Yes, as H** (resolved 2026-09-27).
