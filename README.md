# Crack the Sky

Massive.com (ex-Polygon.io) "Options Developer" ingestion for SPY/SPX vol data.
Sweeps the full option chain every minute, captures delayed option minute bars
over websocket, polls trades on a liquid watchlist, and reconciles daily against
S3 flat files (the flat file always wins). Target host: Ubuntu 24.04 headless,
repo at `~/crack-the-sky`. Python 3.11+; deps: `requests`, `websockets`,
`boto3`, `pyarrow`, `numpy`, `scipy` (no pandas).

Two sibling packages sit at repo root alongside `ingest/`: `marketdata/` (typed,
schema-validated reads of the clean parquet tree, plus OPRA ticker parsing) and
`pricing/` (Black–Scholes–Merton greeks and IV inversion — calculators, not a
surface). A daily 17:00 ET cron (`python -m pricing.drift_check`) re-derives IV
and greeks from the warehouse and pages if ATM identities break (vendor diffs
are diagnostic when present). System handbook (dark, multi-page):
`docs/index.html` (landing) with one page per section; off-repo machine state
in `docs/box-operations.html`.

## The one thing to understand

Two classes of data live here, and they need opposite treatment:

| | Backfillable? | Consequence |
|---|---|---|
| `option_snapshots` (IV, greeks, open interest, underlying price) | **No.** No endpoint returns historical snapshots. | Gone forever if not captured live. Swept every minute; everything else yields API budget to it. |
| trades, minute bars, day bars | **Yes** — S3 flat files reach back to 2014 (trades/day aggs) and 2022 (minute aggs). | A missed day is an inconvenience, not a loss. Re-pull with `scripts/repair.sh`. |

This is why `snapshot_sweep` runs 421x a day and `eod_dayaggs_rest` is not
scheduled at all.

## Quickstart (5 steps, headless box)

1. **Transfer the repo** to the box, e.g.
   `rsync -av --exclude venv --exclude .git Crack-the-Sky/ box:~/crack-the-sky/`
2. **Bootstrap:** `cd ~/crack-the-sky && bash scripts/bootstrap.sh`
   (creates `venv/`, installs deps, seeds `.env` from `.env.example`)
3. **Edit `.env`:** set `MASSIVE_API_KEY` and the flat-file S3 creds
   (`MASSIVE_S3_ACCESS_KEY_ID` / `MASSIVE_S3_SECRET_ACCESS_KEY` from the
   Massive dashboard → S3 Access Keys). Placeholder S3 creds exit code 3.
   Set `HEALTHCHECKS_PING_KEY` too (see Monitoring below) — without it, a job
   that dies dies silently.
4. **Smoke test:** `venv/bin/python -m ingest.entitlements` — probes every
   documented entitlement and validates payload shapes, prints a
   PASS/FAIL/SKIP table.
5. **Install the schedule:** cron does not expand `~`/`$HOME`, so first
   rewrite the placeholder user path, then install:
   ```
   sed -i "s|/home/brad-lasater|$HOME|g" deploy/crontab
   crontab deploy/crontab
   ```

## What the plan actually entitles you to

Probed live rather than assumed. Re-check any time with
`venv/bin/python -m ingest.entitlements` (exits 1 on any drift, and reports
*both* directions — something that stopped working, or something newly
available that no job is capturing).

**Entitled:** options contracts reference (incl. expired), full-chain option
snapshots *with greeks/IV/open interest*, option trades, option aggregates
(minute and day, including same-day), T-1 equity minute aggregates, whole-market
grouped daily bars, dividends/splits, market status, the delayed options
websocket, and the `trades_v1` / `minute_aggs_v1` / `day_aggs_v1` flat files.

**Not entitled** — do not build against these:

| Data | Why it matters |
|---|---|
| Option NBBO quotes (REST **and** `quotes_v1`) | No bid/ask anywhere. The minute snapshot sweep is your only record of option pricing state. (`quotes_v1` is also ~131 GB/day.) |
| Index levels: `I:SPX`, `I:VIX` (and other `I:*` tickers such as `I:VIX9D/3M`, `I:VVIX`) | 403 on every endpoint (`I:SPX`/`I:VIX` verified by the probe), and `underlying_price` is null in the SPX snapshot. SPX spot is recovered from **put-call parity** on the chain instead → `clean/forwards`. |
| Equity trades/quotes, `us_stocks_sip` flat files | — |
| Same-day equity aggregates | `underlying_bars` is therefore T-1 only. |
| `/v1/indicators/*` | Compute locally. |

## Data layout (under `DATA_ROOT`, default `/data/massive`)

```
raw/<dataset>/dt=YYYY-MM-DD/...        # verbatim payloads (JSONL / csv.gz)
clean/<dataset>/dt=YYYY-MM-DD/*.parquet# schema-projected parquet (SCHEMAS)
_meta/                                 # holidays.json, flatfile_manifest.json,
                                       # trades_cursor.json, coverage.json
logs/<job>/dt=YYYY-MM-DD/<epoch>.log   # per-run structured JSONL logs
logs/cron.log                          # combined cron stdout/stderr
```

Datasets: `contracts`, `contracts_expired`, `option_snapshots`, `forwards`,
`option_minute_bars` (src: `ws` | `rest` | `flatfile`; the raw hourly websocket
JSONL lands under the raw-only `raw/option_minute_bars_ws`), `option_day_bars`,
`option_trades`, `underlying_minute_bars`, `underlying_day_bars`, `dividends`,
`splits`. Timestamps are stored exactly as delivered (ns/ms epoch ints, UTC) —
never converted.

Two datasets are **write-only by design** — captured deliberately, read by
nothing yet: `contracts_expired` (Saturday `contracts_sync --expired`; the
survivorship-bias-free record of what was tradable on a past date, kept for
future backtests) and `underlying_day_bars` (`grouped_daily`; an independent
SPY daily-close cross-check, redundant with `underlying_minute_bars` and
rebuildable from one grouped-daily REST call). `forwards` is no longer in
this list: `pricing` reads it for the SPX forward chain.

Clean files are named `{job}-{underlying}-{epoch_ms}.parquet`. Readers that
want "the current chain" use `latest_contracts()` / `latest_snapshots()`, which
take the newest file per underlying — reading a whole partition double-counts
(`contracts_sync` runs twice daily) or returns hundreds of redundant sweeps.

## Ops runbook

- **Snapshots (09:30–16:30 ET, 1/min):** both chains sweep concurrently in
  ~13s, so a 60s slot is comfortable; `flock -n` drops any overlap. A 09:05
  pre-open sweep captures the prior session's settled open interest, and a
  16:35 `--eod` sweep closes the day. Raw JSONL is **off** by default here
  (~6 GB/day at this cadence); pass `--raw` if you need the verbatim payload.
- **WS capture (09:25–16:35 ET):** cron line `25 09 * * 1-5`. Restart by
  re-running `venv/bin/python -m ingest.jobs.ws_minute_bars`. Systemd
  alternative: `deploy/systemd/massive-ws-minute-bars.service`
  (`Restart=on-failure` + `RestartPreventExitStatus=0`, so it does not spin
  overnight when the job exits cleanly outside the window).
- **Trades watchlist (every 5 min):** ~8,000 liquid contracts across SPY, SPX
  and SPXW, polled through a thread pool (`TRADES_CONCURRENCY`, default 8)
  whose total request rate is bounded by a shared token bucket
  (`MASSIVE_MAX_RPS`, default 40). Cursors in `_meta/trades_cursor.json` mean
  only the cold start is slow. This is a same-day convenience — `trades_v1` the
  next morning is more complete.
- **Underlying (T-1 only):** `underlying_bars` at 08:05 and `grouped_daily` at
  08:10 (one REST call returns all 12,518 US tickers). Same-day SPY minute aggs
  are 403 on this tier.
- **Flat files / backfill:** `bash scripts/backfill.sh 2022-08-15 2026-08-27`.
  Newest-first, resume-safe via `_meta/flatfile_manifest.json`, aborts below
  `MIN_FREE_GB` (default 100). Dates outside the history window log
  `flatfile_not_entitled` and are skipped, not fatal. Run it detached:
  `systemd-run --user --unit=massive-backfill bash scripts/backfill.sh ...`
- **Reconcile:** rewrites the clean `option_minute_bars` partition from the flat
  file. Runs daily 11:30 Tue–Sat.

## Did it actually capture everything?

`coverage_audit` is the only thing that reports on the *absence* of a job.
Runs 12:30 Tue–Sat and exits non-zero on any gap (which pings Healthchecks
`/fail`).

```
venv/bin/python -m ingest.jobs.coverage_audit                # previous trading day
venv/bin/python -m ingest.jobs.coverage_audit --date 2026-08-28
```

It checks sweep counts against what the schedule implies (derived from
`market_gate`, so early closes are handled), the largest hole between sweeps,
flat-file manifest completeness, non-empty partitions, websocket output, and
**per-underlying ticker counts** — including that SPY, SPX *and* SPXW all
appear, and that no foreign roots (`SPXL`, `SPYG`, …) leaked in.

**To fix a gap:** `bash scripts/repair.sh 2026-08-28` (re-pull → reconcile →
re-audit). Snapshots are the exception: they cannot be repaired after the fact.

## Monitoring

Every job gets **its own** Healthchecks.io check, named `massive-<job>`. This is
the whole point: one shared check reports green the moment any single job
succeeds, so nine dead jobs hide behind one healthy one — and "this job stopped
running", the failure that actually happens here, cannot be detected at all.

Each run pings `/start` first (so a hung run alerts, not just a crashing one)
and `/fail` with the error text on exception. `ws_minute_bars` also fails its
check when a capture window ends with **zero rows**, since a silent feed is a
failure that looks like success.

One-time setup:

```
# 1. Put BOTH keys in .env. They are different strings from different pages,
#    and swapping them fails silently -- a wrong ping key just 404s.
#    HEALTHCHECKS_PING_KEY=...   (Settings -> Ping Key)      read by every job
#    HEALTHCHECKS_API_KEY=...    (Settings -> API Access)    read by step 2 only
#    Don't identify them by prefix; Healthchecks has issued management keys as
#    both hcak_... and hcw_..., so go by which page you copied it from.

# 2. Create the checks, with schedules matching deploy/crontab. Reads
#    HEALTHCHECKS_API_KEY from .env. --api-key still overrides, but avoid it:
#    a key on the command line is copied into logs you don't control. Running
#    this over Tailscale SSH put the full key in the systemd journal, because
#    tailscaled logs the remote command line verbatim.
venv/bin/python scripts/setup_healthchecks.py --dry-run
venv/bin/python scripts/setup_healthchecks.py

# 3. Add a notification channel in the Healthchecks UI, or nothing reaches you.
```

`scripts/setup_healthchecks.py` is idempotent and carries the schedule and
grace period per job. Tests assert that its job list matches the
`ingest.jobs.*` lines in `deploy/crontab` exactly, so adding a cron line for a
Python job without a check fails CI. `eod_dayaggs_rest` is deliberately in
neither. The one scheduled job without a check is the monthly
`scripts/prune_raw.sh` (a bash line, not an `ingest.jobs` module) — it fails
loudly into `logs/cron.log` and never deletes anything irreplaceable, so it is
deliberately unmonitored.

Self-hosting instead? `HEALTHCHECKS_BASE` is the **ping root**, so set it to
`https://<host>/ping` (hosted `https://hc-ping.com` already is one), and point
the setup script at your management API with
`--api-base https://<host>/api/v3`.

## Development

```
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest tests/        # offline, fixture-based
venv/bin/ruff check ingest tests
venv/bin/python -m compileall ingest    # syntax check
```

Pytest covers all four packages (`ingest`, `marketdata`, `pricing`, plus the
scripts tests). Ruff lints `ingest` and `tests`; the syntax check
(`compileall`) covers `ingest` only — matching both CI pipelines.

**CI runs in two GitHub Actions workflows, deliberately:**

- **GitHub-hosted** (`.github/workflows/ci.yml`) — every push and PR. No API
  key and no `/data/massive`, so it runs only the offline fixture tests, lint
  and a syntax check. This is the PR gate: branch protection on `main`
  requires it and routes all changes through a PR.
- **Self-hosted runner on the ingest box** (`.github/workflows/box.yml`) —
  the box has the credentials and the real data tree. It runs the live
  entitlement probes, a crontab-drift check (`crontab -l` vs
  `deploy/crontab`), and `coverage_audit` — on every push and PR, and on a
  **daily schedule**, which is what turns the coverage audit into an alarm.
  (The runner holds only read-only market-data credentials; see the accepted-
  risk note in the workflow header.) Self-hosted runners are free, including
  on private repos. One-time setup:
  repo → Settings → Actions → Runners → New self-hosted runner, install as a
  service, and add `EnvironmentFile=<repo>/.env` to the service so the checks
  see `MASSIVE_API_KEY` and the S3 creds.

## Retention

`scripts/prune_raw.sh --apply` (monthly via cron) drops raw payloads older than
`RETAIN_DAYS` (default 90) **only** for datasets rebuildable from flat files,
and only once the replacing flat file is recorded in the manifest with rows
kept. One exception: `raw/underlying_day_bars` is also pruned, with no manifest
requirement — it is rebuilt by the daily `grouped_daily` REST call, not a flat
file. It never touches `clean/`, `raw/option_snapshots`, `raw/flatfiles`, or
reference data. Run it without `--apply` to see what it would do.

## Security

- `.env` is gitignored; never commit it. Keep this repo **private** (it
  documents your endpoints and plan tier).
- Rotate `MASSIVE_API_KEY` and the S3 access keys periodically from the
  Massive dashboard; update `.env` and restart the WS job afterwards.
- Keys are only ever sent to `api.polygon.io`, `delayed.massive.com` and
  `files.massive.com` over TLS.
