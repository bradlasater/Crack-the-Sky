# Plan — make `flatfile_pull` retry a missing dataset

Written 2026-09-19 against `main` at `1b36dd2`, after repairing the
`2026-09-14` hole by hand. Scope: `ingest/jobs/flatfile_pull.py`, its tests,
and the `restart` block for `massive-flatfile-pull` in `deploy/schedule.json`.

Status: **decision 1 resolved as A + C; steps 1, 2, 3, 5 and 6 landed in the
working tree, plus an unplanned step 3b (finding 3). Step 4 landed as the
sweep. The box has not been converged — the rendered units are verified
against `~/.config/systemd/user/` but not applied.**

---

## The failure this is fixing

On 2026-09-15 the 11:05 ET run of `flatfile_pull` targeted `2026-09-14` and
ended:

```json
{"event": "job_end", "job": "flatfile_pull", "duration_s": 3310.097,
 "datasets_ok": 1, "datasets_missing": 2}
```

`trades_v1` landed; `minute_aggs_v1` and `day_aggs_v1` did not, because the
vendor had not published them yet. The job **exited 0**. Nothing alerted, the
timer does not look back, and the job only ever targets T-1 — so `2026-09-14`
was left permanently half-written and the archive carried a `PARTIAL` day for
four days.

It surfaced three times before anyone saw it, each time as a *downstream*
failure rather than as the pull's own:

| date | job | error |
|---|---|---|
| 09-15 16:00 | `term_structure` | `no term structure for 2026-09-14: no option_day_bars` |
| 09-15 16:15 | `surface` | `no surface for 2026-09-14: no option_day_bars` |
| 09-15 16:30 | `coverage_audit` | `flatfile[minute_aggs_v1]: not in manifest` |
| 09-19 13:00 | `history_audit` | `HistoryGapError: ... missing: 2026-09-14` |

A `PARTIAL` is the expensive kind of hole. `history_audit`'s own docstring
says why: a whole-partition read still returns rows, so nothing downstream
errors — it just quietly computes on two thirds of a day.

---

## Finding 1 — three retry layers exist, and none of them engaged

1. **In-job**, `_head_with_retry`: on a 404 for T-1 it sleeps
   `RETRY_SLEEP_S` (300s) and re-HEADs until `RETRY_UNTIL_ET` (12:00 ET),
   then logs `flatfile_not_ready_giving_up` and returns `False`. This is the
   3310s in the log above: it waited from 11:05 to 12:00 and gave up.
2. **In-run**, `run_job` / `_retry_policy`: 3 attempts, 30s base backoff —
   but only for exceptions `_is_retryable` recognises (transient HTTP, socket,
   throttling, `BotoCoreError`). A clean `return` never reaches it.
3. **systemd**, `massive-flatfile-pull.service`: `Restart=on-failure`,
   `RestartSec=120`, `StartLimitBurst=3`, `StartLimitIntervalSec=1800`.
   Never fired, because the exit code was 0.

Layers 2 and 3 are both gated on a **non-zero exit**. That is the whole
reason the hole was silent, and it is the thing to change.

## Finding 2 — `datasets_missing` conflates four cases, and three must not retry

`_pull_dataset` returns `None` — which is all `datasets_missing` counts — in
four situations that are not alike:

| # | log event | cause | retry? |
|---|---|---|---|
| a | `flatfile_not_ready_giving_up` | T-1, past 12:00 ET, vendor late | **yes** — this is 09-14 |
| b | `flatfile_absent` | historical date, `wait_for_publish=False` | no — genuinely does not exist |
| c | `flatfile_not_entitled` | object above the tier | no — will never succeed |
| d | `flatfile_dry_run` | `--dry-run` | no — *nothing* was meant to be pulled |

Case (d) is the trap: under `--dry-run` **every** dataset returns `None`, so
`datasets_missing == 3` on every dry run. A blanket
`if datasets_missing: raise` turns `--dry-run` into a guaranteed failure that
pings Healthchecks `/fail`. Cases (b) and (c) would alert forever on a
backfill of a date the vendor never published.

So the retry must key on the *reason*, not on the count.

(One claim in the first draft of this plan was wrong: nothing outside
`flatfile_pull.py` reads the `datasets_missing` / `datasets_ok` fields --
`coverage_audit` reads the manifest, not the job_end line. The fields are kept
stable anyway, because they are what the incident was read off, and `_main`
now logs them in a `flatfile_summary` event before raising, since `run_job`
writes `job_error` instead of `job_end` on an exception.)

## Finding 3 — the retry would have multiplied a duplicate-partition bug

Found while sizing the retry budget, and it turns a "nice to have" into a
prerequisite. `reuse_local` skips only the *download*: a re-run still
re-filters the reused bytes and calls `write_clean_table`, which writes a new
timestamped parquet beside the old one. A whole-partition read then counts
both. That is the hazard `refilter.sh` quarantines against in its own header.

It is not theoretical. The manual `repair.sh 2026-09-14` run on 2026-09-19
re-pulled `trades_v1` -- which had landed fine on 09-15; only the two
aggregate datasets were missing -- and left the partition holding
2,861,001 trades **twice**, 5,722,002 in total. `repair.sh` and
`backfill.sh` both invoke the job without `--replace`, so both carry this.
`coverage_audit` did not catch it: `partition[option_trades]` asserts the row
count is plausible, not that it is un-duplicated.

Making a late dataset fail the run would have multiplied that by the whole
restart budget: every one of the 8 retries re-writes each sibling that *did*
land. So step 3b below is not optional.

## Decision 1 (open) — the retry budget does not cover the failure

This is the part worth your call before I write code.

The literal request — non-zero `datasets_missing` ⇒ non-zero exit ⇒ systemd
retries — buys a retry window of **`RestartSec` 120s × `StartLimitBurst` 3 ≈ 6
minutes**, starting at ~12:00 ET when the in-job wait gives up.

On 09-15 the files were still absent at 12:00 ET and were present by the time
anyone looked. Six more minutes would almost certainly **not** have closed
`2026-09-14`. The change as specified reliably buys the *alert* — which is the
thing that was actually missing, and is worth having on its own — but it does
not reliably buy the *data*.

Three ways to make the retry actually cover it:

- **A. Widen the systemd budget.** `restart.sec: 1800` and raise
  `StartLimitBurst` to ~8 in the template: retries then span ~4 hours, to
  ~16:00 ET. Cheap, uses machinery that already exists, and each retry is a
  fresh process holding no lock. Costs: `StartLimitBurst` is currently
  hardcoded in `massive-job.service.j2` and shared by every job, so it has to
  become a per-unit knob; and a unit sitting in auto-restart for four hours is
  ugly in `systemctl --user list-units`.
- **B. Push `RETRY_UNTIL_ET` later.** One constant, e.g. 16:00 ET. Simplest
  possible diff — but the job then sleeps in-process for up to five hours
  holding `/tmp/massive-flatfile_pull.lock`, which `cronjob.sh` uses as the
  overlap guard. The next day's 11:05 fire would be a `job_skipped` no-op if
  it ever ran long. I do not like this one.
- **C. Self-heal on the next run.** Before pulling T-1, re-check the manifest
  for recent session days (say the last 10) that are missing any dataset, and
  pull those first. The 09-16 11:05 run would have closed 09-14 on its own,
  with no alert and no operator. This is the only option that fixes the class
  of bug rather than the instance, and it composes with A — the alert says
  "late", the sweep makes it moot.

**Recommendation: A + C, with the reason-keyed exit from finding 2.** C is the
actual fix; A makes the same-day recovery real; the exit code makes it visible
either way. If you want the smallest change that honours the request, A alone
is defensible and C can follow.

---

## Steps

1. **Classify the miss.** Have `_pull_dataset` return the *reason* alongside
   the entry (or `None` + a reason out-param — a small
   `@dataclass PullResult` is probably cleaner than widening the tuple).
   `_main` keeps emitting `datasets_missing` unchanged — the log contract and
   `coverage_audit` both read it — and adds `datasets_late`, counting only
   case (a).
2. **Exit non-zero on a late dataset.** New `FlatfilePullError(RuntimeError)`
   in the module, matching the `CoverageError` / `HistoryGapError` /
   `SurfaceError` convention. Raise it from `_main` when `datasets_late > 0`.
   Deliberately *not* added to `_is_retryable`: layer 2's 30s backoff is
   useless against a vendor that is hours late, and it would triple the job's
   wall time before systemd ever got a turn. **Gated on decision 1.**
2b. **Skip a dataset that is already landed** (finding 3, unplanned). A
   manifest row is written after the parquet, so its presence is a completion
   record: `_pull_dataset` now returns it untouched instead of re-filtering
   and writing a second file. `--replace` / `--force-download` still rewrite,
   so `refilter.sh` is unaffected; `repair.sh` and `backfill.sh` stop
   duplicating. **Landed.**
3. **Widen the retry budget** (option A): `restart.sec: 1800` for
   `massive-flatfile-pull` in `deploy/schedule.json`, and make
   `StartLimitBurst` a per-unit field in `massive-job.service.j2` defaulting
   to the current 3 so no other unit changes. Re-render and diff every unit
   before applying — the header says *do not edit on the box*.
4. **Backfill sweep** (option C): at the top of `_main`, walk the last N
   session days via `manifest_dates` and pull any dataset missing for a day
   the oracle says was a session. Needs a cap so a cold archive does not
   re-pull years, and must not run under `--dry-run` or an explicit `--date`.
5. **Tests.** `tests/test_flatfile_refilter.py` is the closest existing file.
   Cases: dry-run stays exit 0 with `datasets_missing == 3`; a not-entitled
   miss stays exit 0; a `not_ready_giving_up` miss raises; the sweep re-pulls
   an incomplete recent day and skips a complete one.
6. **Docs.** `docs/ingest.html` describes the pull's retry behaviour; update
   it and the module docstring, which currently says only that a 404 before
   12:00 ET is retried.

## What the numbers in step 3 buy

The in-job wait gives up at 12:00 ET. Start #1 is the 11:05 timer fire; with
`RestartSec=1800` the retries land at 12:30, 13:00 … 15:30, which is 8 starts.
The 9th, around 16:00, exceeds `StartLimitBurst=8` inside the 21600s window,
so the unit goes to `failed` and `OnFailure=` fires the Healthchecks `/fail`
ping. Retries are cheap: on a same-day retry the HEAD resolves `LATE`
immediately rather than sleeping, because the cutoff has already passed.

`StartLimitIntervalSec` had to become a per-unit knob for this to work at all.
Left at the template's 1800s, starts 1800s apart never accumulate inside one
window, the limit never trips, and the unit would retry forever instead of
alerting. `tests/test_schedule.py` now asserts
`interval_sec > burst * sec` for every unit.

## Verification

- `venv/bin/python -m pytest` — 1498 passed, 15 skipped (baseline was 1478;
  the 20 new ones are `tests/test_flatfile_retry.py`).
- `venv/bin/ruff check .` clean.
- Units re-rendered with `deploy/ansible/render_units.yml` and diffed against
  `~/.config/systemd/user/` ignoring comments: **`massive-flatfile-pull.service`
  is the only unit that changes functionally** (`StartLimitBurst` 3→8,
  `StartLimitIntervalSec` 1800→21600, `RestartSec` 120→1800).
- Noted while diffing: the installed units are already behind the repo
  template on an unrelated `cronjob.sh` comment, from a template edit made
  after the last converge. Harmless, but converging will sweep it in.
- The 09-14 duplicate from finding 3 was moved to
  `_quarantine/duplicate-repair/dt=2026-09-14/` (byte-identical md5 to the
  copy kept), restoring `option_trades` for that day to 2,861,001 rows.
- The dry-run, not-entitled and absent-historical paths are covered by tests
  rather than by hand, so they hold under CI: see
  `test_dry_run_misses_everything_and_still_exits_clean`,
  `test_a_not_entitled_dataset_does_not_fail_the_run` and
  `test_an_absent_historical_date_does_not_fail_the_run`.

Still open: the box is not converged, and the real proof is the next late
publish. Monday's 11:05 run is the first live exercise of any of this.
