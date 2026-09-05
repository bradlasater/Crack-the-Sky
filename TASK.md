# Stream C — migrate deploy/crontab to generated systemd timer units (issue #32)

Worktree: `/home/brad-lasater/cts-timers` · Branch: `feat/systemd-timer-units`
Base: `main` (8f6ca2a)

**Draft-PR work brief. Delete this file in the final commit before merge.**

## Step 0 — bootstrap

```
python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt -e .
```

The root checkout's `venv/` is an editable install pointing at `/home/brad-lasater/crack-the-sky`.
Running it from here would silently import the **main checkout's** code and test the wrong tree.

## Read this first: what just landed under you

PR #39 (`fix/expired-contracts-sync-never-runs`) merged into `main` as 8f6ca2a while this branch was
being set up, and this branch is rebased onto it. Two of its commits touch files this stream
rewrites, so know what they did before you edit them:

- `38ab1ba` / `e3da1df` — inject `--force` for `contracts_sync --expired` (Saturday is never a
  trading day, so the market gate killed that job on every run since the line was installed) and
  edit `deploy/crontab` plus `docs/ingest.html`. `tests/test_market_gate.py` gained a general guard:
  **every weekend-only schedule entry must either force past the gate or resolve its date to a
  trading day.** When the schedule moves to `deploy/schedule.json`, that test has to keep working —
  it currently reads the crontab.
- `fc5a3d7` — an autouse fixture in `tests/test_healthchecks.py` holding the market gate open, so
  the eighteen `run_job` tests stop failing on weekends. **Preserve it** when reworking that file.

## Context

`cron` + `flock` is the whole scheduler. In-run retry exists (`JOB_MAX_ATTEMPTS` /
`JOB_RETRY_BASE_S`, from #20), but there is no cross-run retry or backoff: a job that exhausts its
in-process attempts is done until the next cron tick.

The schedule currently lives in **three** places that can drift:

1. `deploy/crontab` — the stated source of truth
2. the hand-maintained `JOBS` dict at `scripts/setup_healthchecks.py:48`
3. hand-written HTML in `docs/ingest.html` (`#ingest-jobs` table, `#ingest-cron`) and
   `docs/box-operations.html` (`#box-schedule`)

What is guarded today is **job names, never timing**:

- `tests/test_healthchecks.py` asserts the crontab's job set and `JOBS` agree, and that slugs match
  `cli.healthcheck_slug` — but it never compares `JOBS`' schedule strings against the crontab lines
  they claim to mirror.
- `tests/test_docs_drift.py:69` asserts every `-m ingest.jobs.<name>` in the crontab appears
  somewhere in `docs/ingest.html`, and `:174` asserts the crontab's `REPO=` basename appears in
  `docs/box-operations.html`.
- `tests/test_market_gate.py` (new in #39) asserts every weekend-only crontab line either forces
  past the market gate or resolves its date to a trading day.

So a job that is scheduled but undocumented fails CI. A job whose **cadence** silently diverges
between the three copies does not: nothing compares a cron expression to anything. That is the gap
`schedule.json` plus the equivalence test in §4 closes, and it is the reason those three test files
have to move rather than simply keep working.

## 1. `deploy/schedule.json` — one canonical definition

**JSON, not YAML**: Ansible reads it natively with `include_vars`, Python reads it with the stdlib,
and `requirements.txt` gains no dependency (there is no PyYAML in the venv today).

One entry per unit, carrying both schedule representations:

```json
{
  "job": "snapshot_sweep",
  "unit": "massive-snapshot-sweep",
  "command": ["-m", "ingest.jobs.snapshot_sweep"],
  "cron": ["05 09 * * 1-5", "30-59 9 * * 1-5", "* 10-15 * * 1-5", "0-30 16 * * 1-5"],
  "on_calendar": ["Mon-Fri 09:05:00", "Mon-Fri 09:30..59:00",
                  "Mon-Fri 10..15:00..59:00", "Mon-Fri 16:00..30:00"],
  "healthchecks": {"schedule": "* 10-15 * * 1-5", "grace_min": 10, "desc": "..."},
  "restart": null
}
```

Carrying `cron` **and** `on_calendar` is deliberate duplication in a single file, and it is
machine-checked (§4). `cron` also keeps the existing crontab-drift step green through the overlap
period, per the issue's step 1.

Jobs whose cron lines differ only in schedule collapse to one service with repeated `OnCalendar=`.
Jobs whose **command** differs get their own unit but keep the same `job` name — so
`massive-contracts-sync-expired.service` still calls `cronjob.sh contracts_sync`, sharing the lock
and the `massive-contracts-sync` healthcheck slug. Same for `snapshot_sweep --eod`.

The full job inventory is in `deploy/crontab`; there are 19 monitored jobs plus the deliberately
unmonitored monthly `prune`.

## 2. Unit templates

New `deploy/ansible/templates/massive-job.service.j2` and `massive-job.timer.j2`. There is no
`templates/` dir today — the one existing unit, `actions-runner.service`, is inline `copy:` content
at `deploy/ansible/playbook.yml:78`. `deploy/systemd/massive-ws-minute-bars.service` is the style
precedent worth reading.

**Service:**

- `Type=oneshot`, `WorkingDirectory=%h/crack-the-sky`, `EnvironmentFile=-%h/crack-the-sky/.env`
- `ExecStart=/bin/bash scripts/cronjob.sh <job> <venv python> <command...>` — **keep
  `scripts/cronjob.sh`.** Its `flock -n -E 99` → `job_skipped` → `exit 0` contract is what makes the
  dual-run overlap safe (§5), and it is what reaches Healthchecks via `ingest/common/cli.py`.
- `StandardOutput=append:/data/massive/logs/cron.log` so nothing downstream loses the log; journald
  still gets unit start/stop/exit status, which is the actual win (`journalctl --user -u`).
- `Restart=on-failure`, `RestartSec=` from the entry's `restart` block, bounded by
  `StartLimitBurst=3` / `StartLimitIntervalSec=1800`. **Only on the low-frequency jobs** — a retry on
  a 1/min `snapshot_sweep` is pointless and the next tick is 60s away. `restart: null` means no
  `Restart=` line at all. (`Restart=on-failure` is permitted with `Type=oneshot`; `always` is not.)
- `OnFailure=massive-alert@%n.service` — a tiny templated unit that curls the check's `/fail`.
  `cli.py` already pings `/fail` on any non-zero exit; this hook covers the case where the process
  never got to run its own ping (OOM kill, unit start failure).

**Timer:** `AccuracySec=1s`, `Persistent=false`, `RandomizedDelaySec=0`.

`Persistent=false` is not a default worth taking silently — cron has no catch-up, and a missed
snapshot sweep is worthless an hour later. `Persistent=true` would stampede the whole day's backlog
after a reboot.

## 3. Everything that reads the crontab must move

- `scripts/setup_healthchecks.py` — replace the hand-maintained `JOBS` dict with a read of
  `deploy/schedule.json`. `slug_for()` and the `unique: ["slug"]` upsert stay as they are.
- `tests/test_healthchecks.py:174` — `_scheduled_jobs()` regexes `deploy/crontab`; point it at
  `schedule.json`. Keep `JOB_PINGED_CHECKS` (`ws_minute_bars_alive` has no line of its own) and keep
  PR #39's autouse market-gate fixture.
- `tests/test_docs_drift.py` — **not mentioned in issue #32, but it parses the crontab in three
  places**: `_crontab_lines()`, `_scheduled_ingest_jobs()`, and `test_box_path_matches_ops_page()`
  (which asserts the `REPO=` basename appears in `box-operations.html`). All three move to
  `schedule.json`.
- `.github/workflows/box.yml:70` — keep the existing crontab-drift diff during the overlap, and
  **add** a timer-drift step: render units from `schedule.json`, diff against what is installed in
  `~/.config/systemd/user/`, and assert every timer in `schedule.json` appears in
  `systemctl --user list-timers`.

## 4. New test: the two schedule representations must agree

`tests/test_schedule.py` — for each entry, assert the `cron` and `on_calendar` expressions fire at
the same instants. Use `systemd-analyze calendar --iterations=N <expr>` for the systemd side and
extend the small cron-field expander already in `tests/test_healthchecks.py` (`_expand`) for the cron
side. Guard with `shutil.which("systemd-analyze")` so a macOS dev box skips; `ubuntu-latest` has it,
so the offline gate really runs this.

This is the test that makes the duplication in `schedule.json` safe, and it is what catches the
genuinely fiddly translations:

| cron | OnCalendar |
|---|---|
| `30-59 9 * * 1-5` | `Mon-Fri 09:30..59:00` |
| `* 10-15 * * 1-5` | `Mon-Fri 10..15:00..59:00` |
| `*/5 10-15 * * 1-5` | `Mon-Fri 10..15:00/5:00` |
| `0-30/5 16 * * 1-5` | `Mon-Fri 16:00..30/5:00` |
| `15 03 1 * *` | `*-*-01 03:15:00` |

Verify each by hand with `systemd-analyze calendar` before trusting the table.

## 5. Why the crontab can stay armed alongside the timers

The plan is to install the units this weekend and leave the crontab installed as a fallback. That is
safe **only** because both paths go through `cronjob.sh` and therefore share
`/tmp/massive-<job>.lock`: whichever fires first runs, the other logs `job_skipped` and exits 0
without pinging anything.

The one hazard is systemd's **default `AccuracySec=1min`**. If the timer fires up to 60s after cron,
cron's ~13s `snapshot_sweep` will have already released the lock and you get a genuine double capture
— two snapshot files in the same minute, which `coverage_audit` reads as stray sweeps.
`AccuracySec=1s` on every timer closes that window to a second or two, well inside any job's runtime.
**This is the single most important line in the templates.**

## 6. Playbook and docs

`deploy/ansible/playbook.yml`: add `include_vars` for `schedule.json`, a loop templating
service+timer pairs into `{{ user_unit_dir }}`, the existing `daemon-reload user units` handler, and
`systemctl --user enable --now` per timer. **Leave the `Crontab installed from the repo` task
(line 56) in place** — the issue's step 1 wants the crontab untouched in this PR.

Docs: `docs/ingest.html` `#ingest-cron` and the `#ingest-jobs` table; `docs/box-operations.html`
`#box-schedule` plus a new systemd-timers section next to the existing runner section. While there:
`#box-healthchecks` claims "Fifteen checks" and there are 19.

## Verify

```
venv/bin/ruff check ingest marketdata pricing tests scripts
venv/bin/python -m compileall -q ingest marketdata pricing
DATA_ROOT=$(mktemp -d) TZ_NAME=America/New_York venv/bin/python -m pytest tests/ -q
```

On the box, after merge:

```
ansible-playbook -i deploy/ansible/inventory.ini deploy/ansible/playbook.yml
systemctl --user list-timers 'massive-*'
systemd-analyze calendar "Mon-Fri 09:30..59:00"
systemctl --user start massive-history-audit.service
journalctl --user -u massive-history-audit -n 50
```

Then force a failure (bad `EnvironmentFile`) and confirm the backoff retries and the `/fail` ping.

**This weekend's own jobs are the live smoke test** — `contracts_sync --expired` Sat 09:00,
`history_audit` Sat 13:00, `holidays_sync` Sun 07:00, `backfill_underlying` daily 02:30 — all fire
before Monday's session with markets closed.

Crontab stays installed. Watch Healthchecks through Monday for double pings or gaps, and check
`coverage_audit` for stray sweeps.

## Out of scope

Removing `deploy/crontab` — that is a **second, later PR** after a clean day of runs, per the issue.
Adding a healthcheck for the unmonitored monthly `prune` job (an open IMPROVEMENTS.md item) is
tempting here but is scope creep.

## Coordination

- Fully disjoint from Streams A (`cts-amer-iv`) and B (`cts-svi`) — no shared files. Can land in any
  order relative to them.
- Blocked only on PR #39 merging, for the rebase.
- Commit prefix convention: `feat:` / `fix:` / `test:` / `docs:`, lower-case imperative.
