# Plan — adopt the trading-day calendar in `pricing/`

Build plan for `PLAN.md` Week 1 item 2. Written 2026-09-06 against `main` at
`8c4a422`. Scope: make time-to-expiry trading-day aware, before the HAR-RV
forecast (item 4) and the event replay (item 7) bake ACT/365 in deeper.

Status: **steps 1-2 landed, step 3 half-landed; the rest needs the owner
decisions below.**
`pricing/calendar.py` and `pricing/daycount.py` ship with 33 tests between
them, and no number has moved yet. Building step 1 turned up three further
findings (3-5) that change what steps 3 and 4 can do: step 3 is smaller than
this plan first assumed, step 4 is larger, and neither can cover the whole
book with today's sources.

---

## What `PLAN.md` assumed, and what is actually there

> "`_meta/trading_days.json` is built and verified; adopting it changes every
> T and theta in `pricing/`. Do it before the forecast and backtester bake
> ACT/365 deeper in."

That reads as a drop-in: the calendar exists, point `pricing/` at it. It is
not. Findings 1 and 2 were on record before any code was written; 3, 4 and 5
came out of building step 1.

### Finding 1 — the verified calendar is backward-looking only

`/data/massive/_meta/trading_days.json` is a `{date: was_a_session}` dict,
1048 entries, **2022-08-31 → 2026-09-04, with zero dates after today**. That
is by construction: `ingest/jobs/history_audit.py` accumulates it by asking
the vendor whether a flat file exists for a past date. It is a *historical
verification artifact*, not a trading calendar.

Pricing needs the opposite direction. Counting sessions from today to an
expiry 5–45 days out is entirely a question about *future* dates, and this
file answers none of them.

The forward-looking source already in the tree is
`/data/massive/_meta/holidays.json`, written by `holidays_sync` from
`/v1/marketstatus/upcoming`: 24 records spanning **2026-09-07 → 2027-07-05**,
comfortably past the 45-DTE horizon. So the pieces exist, but the item needs
a small new component that neither file is today: a session calendar that
answers "is date D a session?" in both directions, by unioning the verified
history with the weekday rule plus the forward holiday list.

Note the first record in that file: **2026-09-07 — tomorrow — is Labor Day.**
A useful first test case.

### Finding 2 — there are already two different T conventions

They disagree today, and any business-day change has to answer for both.

| Path | Where | Formula | Precision |
|---|---|---|---|
| Live / snapshot pricing | `pricing/from_market.py:214` `year_fraction` | ACT/365 from the as-of instant to the **settlement instant** (16:00 ET SPY/SPXW; 09:30 ET AM-settled SPX and VIX) | sub-day, intraday-precise |
| Day-bar derived | `pricing/term_structure.py:171`, `pricing/surface.py:728` | `T = (expiry - session_date).days / 365.0` | whole calendar days |

Callers of the first: `from_market.py:272, 290, 330, 358, 645, 701`.
The `DAYS_PER_YEAR = 365.0` in `term_structure.py:66` carries a comment
already flagging the gap — *"252 is a constant elsewhere in the repo, not a
calendar, so nothing here is trading-day aware."*

That constant is `TRADING_DAYS_PER_YEAR` in `pricing/conventions.py:27`, and
it is worth being precise about what it does: it only rescales **theta output
units** in `apply_conventions` (`per_trading_day` divides by 252). It has
never touched the T that goes into a pricing formula. So "adopt the calendar"
is not a matter of switching 365 to 252 — those are different quantities.

### Finding 3 — the real split is vol time vs money time

`DAYS_PER_YEAR` is doing two unrelated jobs, and only one of them converts:

| Site | Job | Business-day T? |
|---|---|---|
| `surface.py:728`, `term_structure.py:171` | `T` for σ√T | **yes** |
| `surface.py:709`, `term_structure.py:158` | rate-curve tenor lookup | no — money time |
| `signals/spot.py:88` | dividend PV discounting | no — money time |
| `from_market.py:214` `year_fraction` | **both at once** | see below |

The day-bar path already keeps these apart: `_rate_for_expiry` computes its
own tenor independently of the fit `T`. So **step 3 is close to a two-line
change**, not the sweep this plan first implied.

The live path conflates them. `from_market.py:272` takes one `T` from
`year_fraction` and hands it to both `resolve_r(quote, r, T)` and
`eng.price(S, K, T, rate, sigma, ...)`, where it serves σ√T and e^(−rT)
simultaneously. Business-day T there would silently shorten the discount
factor and pick the wrong point on the Treasury curve. Decision 2 is
therefore better framed as **"split `year_fraction` into two functions"**
than as "pick a fractional-first-session rule" — the fractional-session
question only applies to the vol half. `signals/spot.py:88` is listed above
because it is a third site the original table missed; it must *not* convert,
and naming it here is cheaper than someone later converting it by
pattern-matching on the constant.

### Finding 4 — the forward horizon does not cover the long tail

`holidays.json` reaches 2027-07-05, roughly ten months out. The landed
datasets reach much further: on 2026-09-04 the maximum DTE in both
`atm_term_structure` and `vol_surface` is **1932** (SPX LEAPS, ~5.3 years).

Measured across the whole archive rather than one session — the first cut of
this finding quoted 13%/15% from 2026-09-04 alone, which overstates it badly,
because a 2023 session's long-dated expiries are attested history by now:

| Dataset | Rows past the horizon | Rows in the 5-45 DTE book past it |
|---|---|---|
| `atm_term_structure` | 3,722 / 101,467 (**3.7%**) | **0 / 42,953** |
| `vol_surface` | 1,587 / 36,201 (**4.4%**) | **0 / 16,797** |

Worst single session: 13.7% and 15.5%, both recent. So the horizon binds only
on the LEAPS tail, and **never** on the book the strategy trades. That makes
the hybrid in decision 4 cheap rather than a compromise.

### Finding 5 — early-close history is recorded nowhere

`trading_days.json` records whether a past date *was* a session, never how
long it ran, and `holidays.json` only carries early closes inside its
upcoming window. So the four `early-close` records visible today are the
only ones the archive has: a half-day weighting (decision 6) is not
computable over the backtest period from anything currently on disk.
`SessionCalendar.is_early_close` raises outside the window rather than
reporting a past half day as a full one, which keeps the gap visible.

### Finding 6 — the convention stamp forces an atomic rebuild

`marketdata/catalog.validate_arrow_schema` is fail-loud on *both* extra and
missing columns (`catalog.py:191`). Adding a `daycount` column to
`atm_term_structure` and `vol_surface` therefore makes all 1,656 existing
partitions unreadable the moment the schema lands — `load_surface`,
`coverage_audit`, `drift_check` and the two daily jobs all start raising
`SchemaError` until every partition has been rewritten.

So step 3 is not "a two-line change plus a stamp". The schema change, the full
archive rebuild, and the deploy have to land as one operation, and the
scheduled `term_structure` (Tue-Sat 12:00), `surface` (12:15) and
`coverage_audit` (12:30) jobs must not fire in between. Options are in decision
5 below. Making the column nullable and tolerating its absence would dodge this
— and would also reintroduce exactly the unstamped rows the stamp exists to
prevent, so it is not really an option.

### Finding 7 — the SVI archive is not reproducible, which breaks the measurement

Rebuilding one session with identical code and inputs but a different BLAS
thread count returns different parameters. Measured on 2026-09-04, 1 thread
against 8: **397 of 720 values moved**, `svi_rho` by up to 2.24e-03 relative,
`svi_a` and `svi_m` by ~2e-05 — while `rms_error` moved by less than 1e-09.
The optimiser lands elsewhere in a flat basin: the fit is equally good, the
parameters are not the same numbers. `atm_term_structure` is unaffected, being
a scalar Brent inversion with no BLAS underneath.

This bites step 3 directly. "Quantify the real distribution across the archive
before merging" means diffing a rebuild against the existing archive, and that
diff is dominated by this noise unless the thread count is pinned first. It
also means the backtester cannot reproduce the inputs a decision was made on.
Logged in `IMPROVEMENTS.md`; **the pin should land before the step-3 rebuild,
not after**, or the measurement is not worth taking.

---

## Decisions needed before coding

1. **Scope — both paths, or only the derived path?**
   Recommendation: **both, but not in one PR.** Do the day-bar path first
   (`term_structure`, `surface`), because it is whole-day already, its output
   is rebuildable from scratch, and it is what the forecast and backtester
   consume. The live path is intraday-precise and has more callers; it should
   follow as its own change.

2. **What is a business-day year fraction, exactly?**
   The straightforward definition is `T = sessions_remaining / 252`, counting
   sessions in `(session_date, expiry]`. The open question is the live path:
   an intraday as-of stamp needs a fractional first session, and there is a
   real choice between counting the current session as a fraction of its own
   length versus flooring to whole sessions. Needs a call.

3. **Do we keep ACT/365 available?**
   Recommendation: **yes.** Make the convention explicit and selectable rather
   than swapping the constant, so the backtester can re-run history under
   either and the change is measurable rather than assumed. This also means
   every landed `atm_term_structure` / `vol_surface` row should record which
   convention produced it — otherwise old and new rows silently mix.

4. **Long-dated expiries past the calendar horizon** (from finding 4).
   Options: keep ACT/365 beyond the horizon and stamp the convention per row
   (a hybrid — defensible, since vol time matters most at the short end where
   weekends dominate, and the 5-45 DTE book is entirely inside the horizon);
   extend the calendar with an rrule-based synthetic tail (reintroduces
   exactly the guessing step 1 refuses to do); or restrict the landed
   datasets to the horizon. Recommendation: **hybrid, stamped per row.**

5. **Cutover for the schema change** (from finding 6). Options: (a) merge, then
   rebuild in place and accept a multi-hour window where every reader raises;
   (b) rebuild into a staging `DATA_ROOT` under the new code, then swap the two
   `clean/` subtrees and deploy — readers see the old archive until the swap,
   and the swap is a rename; (c) drop the stamp and lose the ability to tell
   the conventions apart. Recommendation: **(b)**, run outside the 12:00-12:30
   job window, with the `prune`/`coverage_audit` timers stopped for the swap.
   This touches production data on a live box, so it is an owner call, not a
   code one.

6. **Half days.**
   `holidays.json` carries 4 `early-close` records (13:00 ET) alongside 20
   `closed`. Two consequences: a business-day count may want to weight an
   early close below 1.0, and — separately — `expiry_instant`
   (`from_market.py:200`) hardcodes 16:00 ET for PM-settled roots, so it is
   already slightly wrong on those dates. That is a pre-existing bug this work
   would surface; worth fixing in the same pass or logging in
   `IMPROVEMENTS.md`.

---

## Proposed sequence

Each step lands runnable with tests, in its own PR.

**Step 1 — `pricing/calendar.py`, the session calendar. — DONE.**
`SessionCalendar` answers `is_session(d)`, `sessions_between(a, b)` (sessions
in the half-open `(a, b]`, so chained intervals add up) and `is_early_close(d)`.
It unions attested history with weekday-minus-holidays over a bounded forward
window and raises `CalendarRangeError` outside both. `load_calendar` /
`save_calendar` / `CALENDAR_NAME` moved from `ingest/jobs/history_audit.py`
to `ingest/common/market_gate.py` — the module that already owns
`holidays.json` — and are re-exported from `history_audit` for
`scripts/build_*.py`. `market_gate` stays stdlib-only.

Three things worth knowing, none of them in the original sketch:

- **Weekends are answered before either source is consulted.** Saturday and
  Sunday are never sessions, so asking a source about them would invent a
  coverage gap. This matters because `history_audit` (Sat 13:00, verifying
  through Friday) and `holidays_sync` (Sun 07:00) hand off across a weekend:
  in the healthy steady state the only date falling between attested history
  and the forward window is that Saturday. A *weekday* in the gap means a job
  is behind, and that raises.
- **The forward window opens on the fetch date, not the first record.** A
  record list starting in November proves nothing about October unless it was
  fetched before October; fetched after, a passed closure has simply dropped
  off `/v1/marketstatus/upcoming` and the weekday rule would call it a
  session. The fetch date is read from the newest `raw/holidays/dt=`
  partition — the raw zone is never rewritten and `prune_raw.sh` keeps this
  dataset by name.
- **`is_early_close` raises outside the window** rather than reporting a past
  half day as a full one (finding 5).

Tests: 22 in `tests/pricing/test_calendar.py`, against verbatim snapshots of
the box's own `_meta` files committed as fixtures — so the 1048-day agreement
pin and the real Labor Day / Thanksgiving / early-close cases run offline in
GitHub CI, not only on the box.

**Step 2 — make the convention explicit. — DONE.**
`pricing/daycount.py` defines `DayCount` with two implementations —
`CalendarDays` (`act/365`, the default) and `TradingSessions` (`bus/252`, on a
`SessionCalendar`) — plus `discount_year_fraction`, the single spelling of
money time. `build_rows` and `build_surfaces` (and both `build_for_date`
wrappers) take `daycount=DEFAULT_DAYCOUNT`; the rate tenor in each now calls
`discount_year_fraction` so the money-time sites are named rather than
merely commented. `signals/spot.py`'s dividend discount is labelled the same
way, since finding 3 flags it as the site most likely to be converted by
mistake.

Proof it is a no-op: the existing suite is unchanged and green, and rebuilding
2026-09-04 through both `build_for_date`s reproduces the landed archive
**bit-identically** — 105/105 `atm_term_structure` rows and 60/60
`vol_surface` rows matching exactly on `t_years`, `rate`, `forward`,
`atm_iv` and all five SVI parameters.

The two tests worth keeping in mind for step 3 are
`test_a_passed_convention_moves_vol_time` and
`test_a_passed_convention_does_not_move_money_time`: passing `TradingSessions`
must move `t_years` to sessions/252 and must leave every tenor handed to
`rate_fn` on ACT/365.

**Step 3 — business-day T on the day-bar path. — HALF LANDED.**

Landed: `HybridSessions` (sessions where the calendar can vouch for the span,
ACT/365 beyond it, per decision 4) and `DayCount.name_for`, which reports the
convention that actually produced one row rather than the convention's own
name — the two differ precisely when the hybrid falls back, and a row that
could not say which it got is the silent mixing this plan is trying to avoid.
`hybrid_for(data_root)` builds it from the box's calendar files. Nothing is
switched over: `DEFAULT_DAYCOUNT` is still ACT/365.

Blocked on decision 5: the schema stamp and the default flip, because finding
6 makes those inseparable from a full rebuild and a production cutover.

The original plan for the rest:
Switch `term_structure` and `surface` to the new convention, stamp the
convention into the landed rows, and rebuild the archive with
`scripts/build_term_structure.py` / `build_surface.py`. Expect IVs to move:
45 DTE is 31–32 sessions, so `31/252 = 0.1230` against `45/365 = 0.1233`
(−0.2%) — but 32 sessions gives `0.1270`, or **+3.0%**, so even the long end
swings either way on one session. The short end is worse, where weekends
dominate: 5 DTE spanning a weekend is `3/252 = 0.0119` vs `5/365 = 0.0137`,
**−13.1% on T**. Quantify the real distribution across the archive before
merging, not after.

**Measured**, over all 97,743 `atm_term_structure` (date, expiry) pairs the
hybrid converts — 3,724 more stay on ACT/365. ΔIV is the shift implied by
holding the observed price fixed, where an ATM option gives σ ∝ 1/√T:

| DTE | pairs | ΔT median | ΔT p5 | ΔT p95 | ΔIV median | ΔIV p5 | ΔIV p95 |
|---|---|---|---|---|---|---|---|
| 0–7 | 10,808 | +3.5% | −42.1% | +44.8% | −1.69% | −16.91% | +31.38% |
| 8–30 | 27,585 | +0.3% | −13.1% | +12.7% | −0.14% | −5.78% | +7.27% |
| 31–45 | 10,075 | +0.3% | −7.2% | +6.7% | −0.14% | −3.20% | +3.78% |
| 46–90 | 9,872 | +0.6% | −5.0% | +3.9% | −0.29% | −1.90% | +2.61% |
| 91–365 | 31,507 | −0.3% | −2.1% | +1.9% | +0.16% | −0.92% | +1.08% |
| 366+ | 7,896 | −0.4% | −1.0% | +0.1% | +0.21% | −0.04% | +0.49% |

The medians are ~0 everywhere, which is the reassuring part: 252/365 and
sessions/calendar-days cancel, so this is not a level shift. The content is in
the *dispersion*. Inside the 5-45 DTE book, IV moves by 3-7% at the tails
purely on where the weekends and holidays fall — which is the artifact the
change exists to remove, now sized rather than asserted. Below 8 DTE it is
violent (±17-31%), which is worth knowing before anything trades that tenor.

Note the surface's own distribution is *not* measurable this way — finding 7
means a refit differs from the landed archive by optimiser noise regardless of
convention, so the thread pin has to land first.

Measured against the box on 2026-09-06, from `pricing.calendar` itself
(as-of Sunday 2026-09-06, so the Labor Day week is in every window):

| DTE | Sessions | `T` at 252 | `T` at 365 | Δ |
|---|---|---|---|---|
| 5 | 4 | 0.0159 | 0.0137 | **+15.9%** |
| 30 | 21 | 0.0833 | 0.0822 | +1.4% |
| 45 | 32 | 0.1270 | 0.1233 | +3.0% |

The sign flips with the window — the −13.1% sketch above assumed a 5-day span
holding 3 sessions, this one holds 4 — which is the point: at the short end T
moves by double digits in whichever direction the weekend falls, and σ scales
as 1/√T, so a 16% move in T is an 8% move in every 5-DTE IV.

**Step 4 — the live path.**
Same convention through `year_fraction`, resolving decision 2. Larger blast
radius: `drift_check` compares own Greeks against vendor Greeks, and the
vendor is on their own convention, so the canary bands
(`fix/canary-reprice-bands`, already tightened once) will need re-derivation.

**Step 5 — docs.**
`docs/pricing.html` and the `conventions.py` docstring both state ACT/365 as
the convention; `tests/test_docs_drift.py` pins prose like this, so the docs
change is part of the work, not a follow-up.

---

## Risks

- **Silent archive mixing.** Rebuilt rows under a new convention alongside old
  rows with no marker is the worst outcome here — every downstream comparison
  becomes meaningless. Step 3's stamp is the mitigation and should land with
  the change, not after.
- **The forward horizon expires.** `holidays.json` reaches 2027-07-05 and is
  refreshed weekly by `holidays_sync` (Sun 07:00). If that job fails quietly,
  the calendar's forward edge creeps toward today and T silently degrades.
  The raise in step 1 turns that into a loud failure, and the job already has
  a Healthchecks ping.
- **Canary noise.** Step 4 will move `drift_check` against an unchanged
  vendor. Budget for re-deriving bands rather than treating the first red run
  as a regression.

## Not in scope

Theta *units* (`conventions.py` already handles per-trading-day output), the
252 constant itself, and anything in the trading-control column.
