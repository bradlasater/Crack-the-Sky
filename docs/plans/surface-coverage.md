# Plan — close the `vol_surface` coverage gap

Written 2026-09-26 against `main` at `0acc0aa`. Found while building
`vol_features` (`docs/plans/features-v0.md`, "Blocked on surface
coverage"), which cannot run over history until this is fixed. Scope:
`pricing/surface.py`, its tests, `coverage_audit`'s `vol_surface` check,
a rebuild of `clean/vol_surface`, and the docs that describe the surface.

Status: **decisions 1–4 resolved 2026-09-26, all as recommended; decision 5
(new, from step 1) open. Step 1 in progress.**

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

**2. Nearly every failure is in a long-dated expiry's deep wing.**

- **Calendar rejections (190).** 97% sit at `|k| > 0.5`, median `|k|` 1.37.
  In 112 of the 185 that name their pair, the earlier expiry is more than 180
  days out; 18 are within a week. The other 5 are the fit-time repair giving
  up ("calendar floor peaks at 32.3 ... no feasible seed"): the floor it was
  asked to clear is the previous slice's extrapolated wing, where `w` reaches
  32, three times the parameter bound.
- **Butterfly rejections (164).** Every failing expiry's fitted range reaches
  past `k = −0.5`; the median left edge is −1.62, the shape of a 181–400 DTE
  expiry (landed slices in that band have median `k_min` −1.66, against −0.39
  for 1–45 DTE). 48 of the 164 have `b` or `ρ` on its bound. Half fail by
  very little (median min g(k) −0.06).
- **Did not converge (34).** The fit exhausts `MAX_NFEV = 5000`. The message
  does not name the expiry, so step 1 records it.

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

What is *not* proposed: loosening the butterfly tolerance, raising the
arbitrage repair budget, or holding a failed slice's parameters from a
neighbour. Each would land a slice that fails a check today.

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

**Step 1 results (2026-09-26).** The diagnostic is committed; the September
refit settled finding 4 and surfaced finding 5. The per-expiry rerun of the
347 sessions is recorded below when it finishes.

**Step 2 — A: independent roots.** `build_surfaces` catches a root's failure,
logs it and continues; `build_for_date` raises only when no root fits.
`coverage_audit`: FAIL on missing SPXW, WARN on missing SPX. Tests: one
root raising still lands the other; no root raises.

**Step 3 — B: overlap-grid calendar check** in `Surface._check_calendar` and
the fit-time floor. Tests: a pair crossing only outside their overlap is
accepted; a crossing inside the overlap is still rejected (the existing
calendar tests must still pass unchanged); a disjoint pair (no overlap) is
accepted with nothing to compare, and says so in the log.

**Step 4 — C: drop-a-slice.** Tests: a non-converging middle slice is
dropped and the chain floors off its predecessor; a session whose every
slice fails still raises; dropped book-tenor slices surface in the audit.

**Step 5 — D, if chosen.**

**Step 5b — E, if chosen:** the curve cut-off in `ingest/common/rates.py`,
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

## Decisions — resolved 2026-09-26

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
5. **Point-in-time curve (open).** Fix the one-day curve look-ahead in this
   plan, before the rebuild (E), or separately? *(Recommended: here, as E.
   The surface rebuild is happening anyway, and doing it without E bakes the
   look-ahead into 1,020 fresh partitions.)*
