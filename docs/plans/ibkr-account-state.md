# Plan — capture IBKR account state, not just fills

Written 2026-09-19 against `main` at `0f1af57`, after IBKR cut market-data
subscriptions for low balance and nothing on the box noticed. Scope: a new
`ibkr_account` job, a Flex client shared with `ibkr_executions`, two new
datasets, a schedule entry, two Healthchecks, a coverage check, and docs.
Phases 2 and 3 are sketched, not designed.

Status: **draft — nothing built. Decision 0 blocks everything; decisions 1–3
need your call.**

---

## What happened

Last week IBKR emailed that the account's market-data subscriptions were
terminated for insufficient balance. The box raised nothing, and that was
correct: its market data comes from Massive, and its one IBKR touchpoint
(`ibkr_executions`, via Flex) is a reporting service that subscription status
does not affect. Verified 2026-09-19: all 22 Healthchecks up, `coverage_audit`
clean for 09-08 through 09-18, and a live Flex pull returned a well-formed
statement for 09-18 (Trades section present, account id matching
`IBKR_ACCOUNT_ID`, zero fills).

So no capture is broken. The gap the incident exposed is that the box has no
view of the account at all, so the only warning was IBKR's email.

## Finding 0 — the repo is public, and `box.yml` assumes it is private

Found while checking what this plan would put on disk. It is outside the plan's
scope and ahead of it in priority.

- `gh repo view` reports visibility **PUBLIC**. No GitHub Pages site is
  configured, so nothing obvious depends on that.
- `.github/workflows/box.yml` runs on `pull_request`, on `[self-hosted, linux]`,
  and the runner service loads `.env` through `EnvironmentFile=`. Every step
  therefore sees `MASSIVE_*`, `HEALTHCHECKS_*` and `IBKR_FLEX_TOKEN`. The
  header's accepted-risk note rests on "private repo with a single owner (only
  the owner can push)" and "no brokerage access or money movement is reachable
  from this box". On a public repo anyone can open a PR from a fork, and the
  fork-PR approval policy is `first_time_contributors`, so only a contributor's
  *first* PR waits for approval. The Flex token cannot move money, but it does
  read full account statements.
- `tests/fixtures/flex_trades.xml` and `tests/test_ibkr_executions.py` carry the
  real IBKR account id. It is not a credential, but it is in public history.

This plan would add balances, and in phase 2 positions, under `/data/massive`
on that same box. That raises the stakes of an exposure that already exists.
See decision 0.

## Finding 1 — IBKR's cutoff rule, and why the box could not see it coming

IBKR's documentation page "Market Data Subscription Minimum Equity Balance
Requirements" says market data is terminated once a client receives a *Market
Data Violator* notice with equity below USD 500. It also says the account
should hold USD 500 **plus** the cost of all its market-data subscriptions.
(IBKR's pages refuse automated fetches, so this comes from the page's
search-indexed text. Check the figure against your email.)

Nothing in the repo reads account value. `ibkr_executions` asks Flex only for
the Trades section, and lands nothing on a day without fills.

## Finding 2 — most of the "blocked on the API" broker inputs are already in Flex

`PLAN.md` marks Broker / account inputs **partial** ("No positions, cash,
margin, and no TWS/Gateway API"), and its decision 3 says ledger and
reconciliation need TWS/Gateway access. Checked against IBKR's Activity Flex
Query Reference, which lists 46 sections, the same read-only token can return:

| Want | Flex section |
|---|---|
| Daily account value | Net Asset Value (NAV) Summary in Base; Change in NAV |
| Cash by currency, fees, deposits | Cash Report; Cash Transactions; Statement of Funds |
| End-of-day positions | Open Positions |
| Assignment / exercise / expiry | Options, Exercises, Assignments and Expirations; Pending Exercises |
| Full cost detail | Commission Details; Transaction Fees; Routing Commissions |
| Instrument ids (conid) | Financial Instrument Information |

Flex does **not** provide the following, so decision 3 still stands for them:
- working, cancelled or unfilled orders (Flex reports executions only)
- intraday margin and excess liquidity (there is no margin section in the
  reference)
- anything live

An end-of-day comparison of the book against the broker does not need the API.

## Finding 3 — the executions job keeps no raw record on a quiet day

`ibkr_executions._main_fn` returns before `landing.write_raw_text` when there
are no trades. No statement since 2026-08-31 has had any, so
`raw/ibkr_executions/` does not exist. The vendor payload, which the module
docstring calls "the record of truth", has never been kept. That is harmless
for an empty Trades section, but wrong for account sections, which have
content every business day. `test_a_day_with_no_fills_writes_nothing_and_succeeds`
pins the current behaviour.

## Finding 4 — timing: T-1 data fetched at 18:30 is about 1.5 days old

`ibkr_executions` runs Mon–Fri at 18:30 ET against a "Last Business Day"
query, so Monday's run lands Friday's statement (the 2026-09-14 log shows
`from_date 2026-09-11`). A balance alert on that cadence trails the balance by
a day and a half. IBKR processes statements overnight, so T-1 should be
available the next morning, which would cut the lag to about half a day. This
is **unverified**; step 0 checks it.

A Tue–Sat T-1 job also has to get past `market_gate.require_trading_day`, which
checks *today's* date. `underlying_bars` handles this by converting
`--prev-trading-day` into `--date` before calling `run_job`
(`ingest/jobs/underlying_bars.py:90`). A new T-1 job must do the same, or its
Saturday run silently does nothing (see "Noticed while planning").

---

## Decision 0 (blocking) — public repo plus self-hosted runner

Pick one before landing any account data:

- **A. Make the repo private.** This restores the premise that `box.yml`'s risk
  note was written for. Nothing on the site depends on the repo being public
  (there is no Pages site). The cost is losing a public link to the code, if
  anything relies on one.
- **B. Stay public, but stop fork code from reaching the box.** Drop
  `pull_request` from `box.yml`'s triggers; `push: branches: ["**"]` already
  covers your own branches, since pushing needs write access. Or guard the job
  with
  `if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository`.
  Also set the fork-PR approval policy to require approval for all outside
  collaborators, and rewrite the accepted-risk note to match.
- **Either way:** replace the real account id in the two test files with a fake
  one. Scrubbing it from history needs a force-pushed rewrite. For an account
  id alone I would not bother, but that is your call.

**Recommendation: A**, unless the code is public on purpose, in which case B.
Land it as its own PR, before anything below.

## Decision 1 — one job or two

- **A. Add sections to the existing query and job.** One request a day. The
  costs: a job and check named `ibkr_executions` would own account state, a
  parse failure in any section would also fail fill capture, and the verified
  query gets edited.
- **B. A new `ibkr_account` job with its own Flex query**
  (`IBKR_FLEX_ACCOUNT_QUERY_ID`), sharing the Flex client code. This leaves the
  verified query untouched and gives the new job its own timer, lock, check
  and failure domain. It also lets the account job run in the morning while
  fills keep their evening slot.

**Recommendation: B.** The whole cost is one more timer (22 → 23) and one more
env var.

## Decision 2 — the floor

- **Measure:** NAV total from the NAV Summary (net liquidation value). IBKR's
  notice says "equity", and NAV total is the closest Flex field.
- **Threshold:** a new `IBKR_EQUITY_FLOOR_USD`. IBKR's line is USD 500 plus
  your monthly subscription cost, so set the floor above that by whatever
  margin gives you time to act. Make it required whenever
  `IBKR_FLEX_ACCOUNT_QUERY_ID` is set. No silent default, because silence is
  the failure this plan fixes.
- **Signal:** a separate Healthchecks check, `massive-ibkr-equity-floor`, owned
  by the job in `extra_checks`. Send a success ping when NAV ≥ floor and an
  explicit `/fail` when it is below. The job itself still exits 0, because it
  captured the data fine. A red `ibkr_account` check should mean the capture
  broke, not that the balance is low.
- **Body:** say "below floor" and give the report date, but **no dollar
  amounts**. The ping body leaves the box for Healthchecks and whatever it
  notifies. The amounts stay in the local log.

**Recommendation: as written.** The floor amount is yours to set.

## Decision 3 — how much to build now

- **Phase 1 (now):** account value and cash, the floor alert, and the raw XML
  kept every day.
- **Phase 2 (when live trading is planned):** Open Positions; Options,
  Exercises, Assignments and Expirations; Cash Transactions; Transaction Fees
  and Commission Details. All added as sections to the phase 1 query.
- **Phase 3 (optional):**
  - a 365-day backfill of fills, if you traded before 2026-08-31
  - a weekly 7-day re-pull that repairs any missed executions day
  - Trade Confirmation Flex for same-day fills

**Recommendation: phase 1 only.** Every phase 2 section is empty until there is
a book, and a parser written against empty sections is a parser written
against guesses.

---

## Steps — phase 1

0. **Create the query (you, in IBKR Account Management).** Go to Performance &
   Reports → Flex Queries and create an Activity Flex Query named "Account State
   Last Business Day":
   - sections: *Net Asset Value (NAV) Summary in Base* and *Cash Report*, all
     fields
   - period: Last Business Day
   - format: XML, with Account ID included

   Put its id in `.env` as `IBKR_FLEX_ACCOUNT_QUERY_ID` and set
   `IBKR_EQUITY_FLOOR_USD`. It uses the same token.

   Then I pull one statement with a read-only probe, the same way the
   executions query was checked on 2026-09-19, and save a redacted copy (with a
   fake account id) as `tests/fixtures/flex_account.xml`. Element and attribute
   names come from that real statement, not from memory. If the probe runs at
   about 08:30 ET on a weekday, it also answers finding 4's timing question.
1. **Split out the Flex client.** Move `FlexError`, `_get`, `_safe_url`,
   `_fault`, `fetch_statement` and `statement_period` from
   `ingest/jobs/ibkr_executions.py` to `ingest/common/flex.py`, and make
   `fetch_statement` take the query id as an argument. No behaviour change:
   `tests/test_ibkr_executions.py` passes with only its imports changed. The
   token-hygiene tests (`test_http_errors_never_carry_the_token`,
   `test_network_errors_never_carry_the_token`) move with the code.
2. **Schemas.** Add two datasets to `ingest/schemas/__init__.py`:
   - `ibkr_nav`: one row per report date, with account id, report date,
     currency, cash, stock, options and total (NAV), plus whatever else the
     fixture shows is populated.
   - `ibkr_cash`: one row per currency from the Cash Report, with starting
     cash, ending cash, deposits and withdrawals, commissions, and other fees.

   The exact columns get fixed in step 0 against the real statement. Extend
   `tests/test_schemas.py`.
3. **The job, `ingest/jobs/ibkr_account.py`.**
   - Resolve the run date to the previous trading day *before* `run_job`'s
     market gate, the way `underlying_bars` does, so the Saturday run actually
     runs.
   - Fetch the statement and require `toDate` to equal that date, otherwise
     raise. A statement that IBKR has not regenerated yet should be retried,
     not reported as a quiet success.
   - Land the raw XML on **every** run.
   - Parse the NAV and Cash Report sections. A missing NAV row for `toDate` is
     an error, since a business day always has one. The executions job's
     "empty is normal" rule does not carry over.
   - Filter by `IBKR_ACCOUNT_ID` with the same exact-match rule as
     `parse_trades`.
   - Compare NAV total to the floor, then ping `massive-ibkr-equity-floor` with
     success or `/fail` as in decision 2. Skip that ping under `--dry-run`, as
     `snapshot_sweep` does for its all-chains check.
4. **Keep the executions raw payload.** In `ibkr_executions._main_fn`, land the
   raw XML before the early return on a no-trades day (finding 3). Flip
   `test_a_day_with_no_fills_writes_nothing_and_succeeds` so it asserts the raw
   file lands and no clean file does.
5. **Schedule.** In `deploy/schedule.json`:
   - a new unit `massive-ibkr-account` at `Tue-Sat 08:30:00`
     (cron `30 08 * * 2-6`)
   - `restart: {sec: 1800, burst: 4, interval_sec: 10800}`. If the statement
     is not ready at 08:30, starts at 09:00, 09:30 and 10:00 retry it, and the
     fifth start at about 10:30 trips the limit and alerts. Tune this once step
     0 answers the timing question.
   - a healthchecks block with `grace_min: 120`
   - `extra_checks.ibkr_equity_floor` with the same schedule and
     `owner: ibkr_account`, so `run_job` settles it on holidays like the other
     job-owned checks

   `tests/test_schedule.py` already enforces that the cron and on_calendar forms
   agree and that `interval_sec > burst × sec`. Re-render the units and diff
   them against `~/.config/systemd/user/` before converging. Then run
   `scripts/setup_healthchecks.py` to give both new checks their schedules.
6. **Coverage.** Have `coverage_audit` check that an `ibkr_nav` partition exists
   for T-1, but only when `IBKR_FLEX_ACCOUNT_QUERY_ID` is set. Add tests to
   `tests/test_coverage_audit.py`. The audit runs at 12:30 ET, after the last
   retry.
7. **Config and docs.**
   - `Settings`: add `ibkr_flex_account_query_id` and `ibkr_equity_floor_usd`,
     and fail loudly at load if the first is set without the second.
   - Update `.env.example`, `docs/knobs.html`, `docs/ingest.html` (job row) and
     `docs/data-flow.html` (two write-only datasets).
   - `docs/box-operations.html`: the second query's configuration, IBKR's USD
     500 plus subscriptions rule, and the accepted-risk note per decision 0.
   - `PLAN.md`: the Broker / account inputs row and the wording of decision 3.

## Verification

- `venv/bin/python -m pytest` and `venv/bin/ruff check .` pass. New tests run
  off the redacted fixture.
- `python -m ingest.jobs.ibkr_account --dry-run` on the box, against the real
  query, logs the parsed NAV date and a floor verdict, lands nothing, and sends
  no extra ping.
- One real run lands the raw XML and both clean partitions for T-1, and both
  `massive-ibkr-account` and `massive-ibkr-equity-floor` go green.
- Prove the floor alert end to end once: temporarily set
  `IBKR_EQUITY_FLOOR_USD` above the real NAV and confirm the Healthchecks
  notification actually arrives, then set it back. This also confirms that a
  notification channel exists at all. box-operations warns that without one,
  alerts go nowhere.

## Phases 2–3, sketched

- **Positions, exercises and costs (phase 2).** Add the sections to the account
  query, with these datasets:
  - `ibkr_positions`, with `conid` and an OPRA ticker rebuilt by the existing
    `opra_ticker`
  - `ibkr_option_eae`
  - `ibkr_cash_transactions`
  - `ibkr_fees`

  The first consumer is an end-of-day check of the book against the broker.
  That is the ledger box in `PLAN.md`, built without the API.
- **Executions history (phase 3).** A second saved executions query with the
  period "Last 365 Calendar Days", run by hand in a `--backfill` mode that lands
  rows per trade date instead of per statement `toDate`. Without that mode the
  period guard would refuse the statement. It is only worth doing if there are
  fills from before 2026-08-31.
- **Weekly repair (phase 3).** The same shape with "Last 7 Calendar Days", on a
  Saturday timer, re-landing any trade date whose fills differ from what is
  stored. It does for executions what `flatfile_pull` already does for flat
  files: repair days a normal run missed.
- **Same-day fills (phase 3).** Trade Confirmation Flex. IBKR's guide says a new
  execution appears within about 5–10 minutes, and third-party reports say up
  to 30. A future kill switch would watch this feed for fills it did not
  expect.

## Noticed while planning (out of scope)

- `rates_sync` is scheduled Tue–Sat but checks the market gate against
  *today*, so its Saturday run always stops at the gate (logs for 09-12 and
  09-19 show `job_start` only). No curve data is lost, because Tuesday's pull
  covers Friday. But with no Saturday or Monday run landing it, anything priced
  on Monday can see at most Thursday's curve. The fix is the `underlying_bars`
  pattern, or dropping Saturday from its schedule.
- `ibkr_executions` fetches "Last Business Day" at 18:30, so fills land one to
  three days after the trade. If step 0 shows T-1 is ready by morning, moving
  it next to the account job at Tue–Sat 08:30 roughly halves that delay.
