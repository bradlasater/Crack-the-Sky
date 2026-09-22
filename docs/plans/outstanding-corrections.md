# Plan — outstanding corrections after the OS update and Saturday's fixes

Written 2026-09-21 (Mon) against `main` at `0d62b76`, after a full audit of the
day's capture. Scope: what is still needed to consider Saturday's two fixes
landed, plus everything else the audit turned up.

Status: **nothing is broken and no data is lost.** Every item below is either a
verification still owed, a latent bug, or housekeeping. Item 1 is a security
exposure and should go first.

**Update 2026-09-21 22:30** — items 2, 3 and 4 are done or in flight on
`fix/rates-sync-weekday-and-dry-run`; item 1 is deferred by Brad. Item 2's
verification turned up a real bug in the #81 code, now fixed in the same branch
(see "What item 2 found"). Item 3 landed as two commits. Item 4 is done apart
from the disk deletions. What remains is merging the branch and converging the
box, which must happen in that order: `deploy/ansible/playbook.yml` refuses a
checkout with local modifications, because the box tracks `main`.

---

## What the audit found clean

Monday 2026-09-21 capture, verified against logs, units and the files on disk:

- `snapshot_sweep`: **423 of 423** scheduled minutes, no gaps, no strays, every
  start within 20 s of its minute. 1,269 parquet files on disk, matching 1,269
  `snapshot_swept` events exactly. `errors: 0` throughout.
- `ws_minute_bars`: 1 connect, **0 reconnects**, full 25,800 s window, 375,878
  events across 17,355 symbols, 8 clean rotations. Event profile identical to
  every session 09-10 through 09-18.
- `drift_check`: **PASS**. `median_abs_reprice` 2.8e-13, `identity_beyond` 0,
  every pair identity (reprice, gamma, vega, PCP) 0. Surface check PASS.
- `trades_watchlist`: 83 of 85 runs. Not a regression — 09-10 through 09-18 ran
  82–85 with the same timer-queue contention.
- `ibkr_executions`: ran, pulled 09-18 (0 fills). The T-1 lag is finding 4 of
  `ibkr-account-state.md`, not a new fault.
- 510 `job_start` / 510 `job_end`, 0 failed units, 0 `job_skipped`.
- Full test suite passes, `ruff` clean, latest scheduled `box` CI run green.

**Archive integrity — both fixes' historical damage is fully repaired:**

- The double-write invariant (manifest `rows_kept` == rows in `flatfile_pull`'s
  own file) holds across **2,032 dataset-days with 0 mismatches**. The two dates
  the commit names, 2026-09-04 and 2026-09-14, each now carry exactly one
  `flatfile_pull` file per dataset.
- **1,016 of 1,016** trading sessions from 2022-08-31 to 2026-09-18 have all
  three flat-file datasets landed. Zero partial, zero empty.

So there is no backfill or repair owed. What is owed is proof the fixes work
unattended.

---

## 1. Public repo plus a credentialed self-hosted runner (blocking)

This is finding 0 of `ibkr-account-state.md`, recorded 2026-09-19 and still
open. Re-verified today.

- `gh repo view` reports visibility **PUBLIC** (0 forks, 1 star, created
  2026-08-23).
- `.github/workflows/box.yml` triggers on `pull_request`, runs on
  `[self-hosted, linux]`, and the runner service loads `.env` through
  `EnvironmentFile=`. Every step therefore sees `MASSIVE_API_KEY`, the S3
  credentials, `HEALTHCHECKS_*` and `IBKR_FLEX_TOKEN`.
- The workflow's own accepted-risk note still reads "private repo with a single
  owner (only the owner can push)". That premise is false.

Anyone can fork and open a pull request, and their workflow code executes on the
box beside those credentials and `/data/massive`. The Flex token cannot move
money, but it reads full account statements. I could not read the fork-PR
approval setting through the API (the endpoint 404s for this repo); the earlier
plan recorded it as `first_time_contributors`, which gates only a contributor's
*first* PR.

**The repo is public by design, and stays public** (Brad, 2026-09-22). It is
linked from his website as a work sample for hiring managers reviewing him for
a quant role, so public visibility is a requirement of the project rather than
an oversight. Earlier drafts of this document recommended
`gh repo edit --visibility private`; that recommendation is withdrawn, and no
future audit should re-raise it.

That leaves exactly one remedy worth taking — stop fork code reaching the box,
which costs nothing a reader of the repo would notice. Either drop
`pull_request` from `box.yml`'s triggers (`push: branches: ["**"]` already
covers your own branches, since pushing needs write access), or keep the
trigger and guard the job with

```yaml
if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
```

Two smaller pieces belong with it:

- The accepted-risk note in `box.yml` **has been rewritten** (2026-09-22) to
  say the repo is public on purpose, to stop claiming a private repo as its
  mitigant, and to name the fork-PR path as the open item.
- Still owed: replace the real IBKR account id in
  `tests/fixtures/flex_trades.xml` and `tests/test_ibkr_executions.py` with a
  fake one. On a repo written to be read by strangers, that one is worth doing
  on its own account.

**Verify:** a test PR from a fork produces no `box` run.

---

## 2. Confirm Saturday's two fixes on their first unattended run

Neither has run on its own schedule yet. Both merged Saturday evening, after
that morning's runs, and neither job fires on Sunday or Monday.

| Fix | State | First scheduled run |
|---|---|---|
| #82 `coverage_audit` double-write check | Exercised by hand twice (Sat 20:11, Sun 20:19 ET), 24/24 PASS, new `flatfile_partition[...]` checks present and passing | Tue 12:30 ET |
| #81 `flatfile_pull` late-file failure + partial-day heal | **Never executed** | Tue 11:05 ET |

The deploy half of #81 is correctly installed — the unit carries
`RestartSec=1800`, `StartLimitBurst=8`, `StartLimitIntervalSec=21600`, and the
timer is `Tue-Sat 11:05:00`, `Persistent=false`.

**What to watch tomorrow**, in `/data/massive/logs/flatfile_pull/dt=2026-09-21/`:

- all three datasets land for 2026-09-21, and the manifest gains three entries;
- the new heal sweep logs its 10-session backfill pass and finds nothing to do
  (the archive is already complete, so a clean no-op is the expected result);
- exactly one `flatfile_pull-*.parquet` per dataset per date — the re-write
  suppression holding;
- then `reconcile` at 11:30 and `coverage_audit` at 12:30 both green, with
  `flatfile_partition[trades_v1]` and `[day_aggs_v1]` PASS.

### What item 2 found

Probing the deployed #81 code by hand turned up a real bug in it, and cost a
false alarm on the way.

`--dry-run` against a date the vendor has not published **exited 1, raised
`FlatfilePullError` and pinged `/fail`** on the production check. That is the
exact regression #81's commit message set out to avoid: it guarded the blanket
form, but `_head_with_retry`'s miss returns from `_pull_one` *before* the
`args.dry_run` branch below it, so `LATE` never reached the exemption. The
existing test only covered objects that exist, where the dry-run branch is
reached before the miss can matter — so the gap was invisible.

A second defect fell out of the same read: `wait_for_publish` sleeps
`RETRY_SLEEP_S` until 12:00 ET, so a dry run *before* noon would sit there for
the best part of an hour to print what the first HEAD already knew.

Both are fixed on this branch, with two tests that fail against the previous
code. The `massive-flatfile-pull` check was reddened by the probe at 22:00 ET
and cleared by hand with a success ping at 22:05.

**Lesson worth keeping:** `Settings.load()` reads `.env`, so running any job
module directly — not through `scripts/cronjob.sh` — still pings production
Healthchecks. Hand-runs belong behind `HEALTHCHECKS_PING_KEY= `, which is the
convention `cronjob.sh` already documents ("Empty-but-set must win so tests
cannot leak a real ping against production").

The LATE path itself (vendor genuinely silent past the cutoff → exit 1 →
`/fail` → `Restart=on-failure`) is covered by `tests/test_flatfile_retry.py`;
forcing it in production is not worth the noise.

---

## 3. `rates_sync` never runs on Saturday, so Monday prices off a stale curve

Noted as out-of-scope in `ibkr-account-state.md`; confirmed today with data.

`rates_sync` is scheduled `Tue-Sat 08:20`, but `main()` calls `run_job` directly
(`ingest/jobs/rates_sync.py:222`) with no date resolution, so `run_job`'s market
gate tests **today's** date and stops on Saturday. The 2026-09-19 log holds a
`job_start` and nothing else — no `job_end`, no data.

With no Saturday and no Monday run, the effect compounds:

- latest `treasury_yields` partition: `dt=2026-09-18`
- freshest curve date inside it: **2026-09-16**

So everything priced today ran against a curve three trading days old. Nothing
alarms, because `coverage_audit` has no treasury or rates check at all.

**Fixed** in commit `c9af837`, but not by the `underlying_bars`
`--prev-trading-day` pattern I first proposed. That pattern resolves `--date` to
T-1, which here would only move the landing partition — `dt=` is the ingestion
run date, not the curve date — and write a second file into a partition that
already had one. `rates_sync` has no T-1 to target: it fetches the latest
published curve whatever the date.

The schedule is now **Mon–Fri**. That is strictly better than the "drop
Saturday" option I described as weaker in the first draft of this document; I
had that wrong. Dropping Saturday alone leaves Monday unserved, while Mon–Fri
both retires the no-op fire *and* adds the Monday run, which is the one that
actually makes Monday's pricing current.

**The coverage check landed with it.** `coverage_audit` gains `rate_curve`: the
newest `treasury_yields` row at or before the audited session, measured in
trading days. The vendor publishes at a steady T-2 — across the twelve runs from
09-01 to 09-18 the gap was exactly 2 trading days every time — so WARN at 3 is
one missed run and FAIL past 5 is a job that has stopped. Trading days, not
calendar days, or every Monday warns on a healthy box.

**Verify after the box converges:** Monday 2026-09-28's `rates_sync` log shows
`rates_written` and `job_end`, and that day's newest curve date is the prior
Wednesday or later. `coverage_audit`'s `rate_curve` check reads PASS at 2
trading days.

---

## 4. Housekeeping

None of this affects capture. Ordered by size.

- **`/data/massive/_quarantine` holds 22 GB — and I was wrong to offer deleting
  it.** `scripts/prune_raw.sh` already manages this tree on a 30-day window
  (`QUARANTINE_RETAIN_DAYS`, default 30), and a dry run of it right now would
  drop **zero** quarantine batches: every batch across all five generations
  dates from 2026-08-31 onward, so the entire 22 GB is inside the retention
  window. It is not stale residue, it is the live undo log for the last three
  weeks of refilters and reconciles, and the monthly prune on the 1st will start
  retiring it on schedule. Deleting it wholesale would throw away that window
  early, to reclaim space on a volume at 33% with ~340 days of runway. **Left in
  place; no action needed.**
- **Superseded dataset copies, 39 MB — removed.** `clean/vol_surface.act365`,
  `clean/vol_surface.pre-blas-stamp` and `clean/atm_term_structure.act365`,
  left from the ACT/365 and BLAS-pin migrations. These *were* genuinely
  unmanaged: they sit in `clean/` under names no code references and `prune_raw`
  never looks at them, so nothing would ever have aged them out.
- **Seven `crontab.backup-*` files in `/data/massive/_meta`**, the newest from
  2026-09-19 16:42 — the backup taken when the crontab was finally removed. The
  crontab is gone (`crontab -l` reports none) and CI now guards its return, so
  these are historical. Left alone: they total a few KB, and the newest one is
  the only record of what the pre-timer schedule actually was.
- **Leftover git worktree — removed.** `.claude/worktrees/snapshot-sweep-per-chain`
  was on `feat/snapshot-sweep-degraded-alert` (94d475a), confirmed fully merged
  into `main` before removal. `git worktree prune` also cleared a stale `/tmp`
  scratchpad entry. The branch ref itself is left in place — deleting branches
  was not asked for, and `git branch -d feat/snapshot-sweep-degraded-alert`
  cleans it up whenever you want.
- **`.claude/` is now in `.gitignore`.** On a public repo an untracked worktree
  there is one careless `git add -A` away from being published.
- **`docs/plans/ibkr-account-state.md` committed**, along with this document, so
  the findings live in history rather than only on this box.

---

## Deployed 2026-09-21 22:45 ET

PR #83 merged as `fb07309`, and the box is converged.

- `ansible-playbook -i deploy/ansible/inventory_local.ini deploy/ansible/playbook.yml`
  changed exactly the one unit predicted: `massive-rates-sync.timer`,
  `OnCalendar=Tue-Sat 08:20:00` → `Mon-Fri 08:20:00`. No failed units.
- The playbook's timer re-arm fired `rates_sync` immediately, which turned out
  to be the useful accident: it landed a curve dated **2026-09-18**, so
  `load_curve(2026-09-21)` now returns Friday's curve instead of the 09-16 one
  the day was actually priced against. Today's staleness is healed, not just
  prevented from recurring.
- `scripts/setup_healthchecks.py` applied; `massive-rates-sync` now carries
  `20 8 * * 1-5`.
- `coverage_audit --date 2026-09-18` reports **25/25 PASS**, the new
  `rate_curve` among them.
- A clean `workflow_dispatch` of `box` on `main` is green on every step,
  including Timer drift — so the repo and the box genuinely agree, rather than
  the merge run having passed on timing.

Worth noting for the threshold: the vendor lag is not *always* exactly 2
trading days. The twelve runs sampled from 09-01 to 09-18 were all 2, but
tonight's landed at 1. Both PASS; WARN still starts at 3, which remains the
right line for "a run did not land".

## Tuesday 2026-09-22 — the unattended runs verified

Item 2's whole purpose. Read off the logs at 11:50 ET, on their own schedules,
with nobody driving them:

- **`flatfile_pull` 11:05** — all three datasets landed for 2026-09-21,
  `datasets_ok: 3`, `job_end` in 37 s. The manifest gained exactly three
  entries, whose `rows_kept` (3,339,841 / 382,569 / 17,895) match the rows the
  run reported writing, and each dataset's partition carries exactly one
  `flatfile_pull-*.parquet`. So #81's late-file handling and #82's
  re-write suppression both hold on a real unattended run.
- **`reconcile` 11:30** — green, 382,569 rows, `reconcile_overwritten`.
- **`rates_sync` 08:20** — `rates_written` and `job_end`, on the first Tuesday
  of the Mon–Fri schedule from #83.
- The only `job_error` in the last two days remains the 22:00 hand-probe of
  2026-09-21 that this document already accounts for.

Live capture was healthy while this was read: `ws_minute_bars` at 1 connect and
0 reconnects, `snapshot_sweep` `errors: 0`, and the session's 13 `ws_silence`
events matching 09-16, 09-17, 09-18 and 09-21 exactly — so that count is the
shape of a normal session, not a new symptom.

### The heal sweep logged nothing, and now does

Verifying the above turned up an observability hole rather than a bug. The
sweep's expected result on a healthy archive is to find nothing — but
`_backfill` returned `{}` and logged nothing in that case, so `job_end` carried
no counters and the log held no trace. "The sweep ran and found nothing" and
"the sweep never ran" were therefore the same log, which is precisely the
question this verification had to answer. (It *was* running: `deploy/schedule.json`
invokes `-m ingest.jobs.flatfile_pull` with no `--date`, so `args.backfill` is
true — but that had to be established from the schedule, not the evidence.)

Fixed: `_backfill` now always logs `flatfile_backfill_swept` with its lookback
window and the dates it found short, and always returns counters. The empty
dict is now reserved for the sweep being *disabled* — an explicit `--date`,
`--no-backfill`, or a dry run — so an absent `backfilled` key in `job_end`
means something different from `backfilled: 0`. Four tests pin it, all of which
fail against the previous code.

## What is left

1. **Item 1 — the fork-PR path to the credentialed runner.** The repo is
   public by design and stays that way (see item 1 above); what remains is
   guarding `box.yml` so fork code cannot run on the box. Still the only item
   with an attacker in the threat model.
2. **The IBKR account id in the test fixtures**, which wants a fake value.
3. **Two scheduled runs still to watch:** today's `coverage_audit` at 12:30,
   and Monday 2026-09-28's `rates_sync`.

## Verification when all of it is done

- Today (Tue 09-22) at 12:30: `coverage_audit` green on a scheduled run, with
  the `flatfile_partition[...]` checks passing and `rate_curve` PASS at 2
  trading days. (`flatfile_pull`, `reconcile` and `rates_sync` are done —
  recorded above.)
- Monday 2026-09-28: `rates_sync` logs `rates_written` and `job_end`, and that
  day's newest curve date is the prior Wednesday or later.
- A test PR from a fork produces no `box` run, whenever item 1 is taken up.
- `venv/bin/python -m pytest` and `venv/bin/ruff check .` pass. (Both did at
  commit time: full suite green, lint clean.)
- `git status` clean apart from what you mean to be there.
