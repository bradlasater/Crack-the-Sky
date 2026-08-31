# data_ingest_infra

Massive.com (ex-Polygon.io) "Options Developer" ingestion for SPY/SPX vol data.
Captures delayed option minute bars over websocket, REST contracts/snapshots/
trades/aggregates on a cron schedule, and reconciles daily against S3 flat
files (the flat file always wins). Target host: Ubuntu 24.04 headless, repo at
`~/data_ingest_infra`. Python 3.11+; deps: `requests`, `websockets`, `boto3`,
`pyarrow` (no pandas).

## Quickstart (5 steps, headless box)

1. **Transfer the repo** to the box, e.g.
   `rsync -av --exclude venv --exclude .git data_ingest_infra/ box:~/data_ingest_infra/`
2. **Bootstrap:** `cd ~/data_ingest_infra && bash scripts/bootstrap.sh`
   (creates `venv/`, installs deps, seeds `.env` from `.env.example`)
3. **Edit `.env`:** set `MASSIVE_API_KEY` and the flat-file S3 creds
   (`MASSIVE_S3_ACCESS_KEY_ID` / `MASSIVE_S3_SECRET_ACCESS_KEY` from the
   Massive dashboard → S3 Access Keys). Placeholder S3 creds exit code 3.
4. **Smoke test:** `venv/bin/python -m ingest.smoke` — 6 live checks,
   prints a PASS/FAIL/SKIP table.
5. **Install the schedule:** cron does not expand `~`/`$HOME`, so first
   rewrite the placeholder user path, then install:
   ```
   sed -i "s|/home/brad|$HOME|g" deploy/crontab
   crontab deploy/crontab
   ```

## Data layout (under `DATA_ROOT`, default `/data/massive`)

```
raw/<dataset>/dt=YYYY-MM-DD/...        # verbatim payloads (JSONL / csv.gz)
clean/<dataset>/dt=YYYY-MM-DD/*.parquet# schema-projected parquet (SCHEMAS)
_meta/                                 # holidays.json, flatfile_manifest.json,
                                       # trades_cursor.json, ...
logs/<job>/dt=YYYY-MM-DD/<epoch>.log   # per-run structured JSONL logs
logs/cron.log                          # combined cron stdout/stderr
```

Datasets: `contracts`, `option_snapshots`, `option_minute_bars` (src column:
`ws` | `rest` | `flatfile`), `option_day_bars`, `option_trades`,
`underlying_minute_bars`, `dividends`, `splits`. Timestamps are stored exactly
as delivered (ns/ms epoch ints, UTC) — never converted.

## Ops runbook

- **WS capture (09:25–16:35 ET):** cron line `25 09 * * 1-5`. Restart by just
  re-running `venv/bin/python -m ingest.jobs.ws_minute_bars` (flock in cron
  prevents doubles). Systemd alternative: `deploy/systemd/massive-ws-minute-bars.service`
  (user unit; enable lingering). Reconnects/gaps are logged as `ws_gap`
  events in the run log; hourly JSONL is gzipped on rotation.
- **Flat files / backfill:** `bash scripts/backfill.sh 2026-08-01 2026-08-31`
  loops `flatfile_pull` over the range and skips dates already in
  `_meta/flatfile_manifest.json` (resume-safe). Single day:
  `venv/bin/python -m ingest.jobs.flatfile_pull --date 2026-08-29`.
  Default `--date` (no flag) = previous trading day. Exit 3 ⇒ fix S3 creds
  in `.env`.
- **Reconcile:** `venv/bin/python -m ingest.jobs.reconcile --date 2026-08-29`
  logs WS-vs-flatfile deltas (rows/tickers/volume) and rewrites the clean
  `option_minute_bars` partition with the flat-file rows. Runs daily 11:30
  Tue–Sat via cron.
- **Fix gaps:** missing intraday data for a day ⇒ run `flatfile_pull` +
  `reconcile` for that date; missing contracts ⇒ re-run `contracts_sync`.
- **Where logs live:** `{DATA_ROOT}/logs/<job>/dt=<date>/` (JSONL, grep for
  `"event":"job_error"`), plus `{DATA_ROOT}/logs/cron.log`.

## Security

- `.env` is gitignored; never commit it. Keep this repo **private** (it
  documents your endpoints and plan tier).
- Rotate `MASSIVE_API_KEY` and the S3 access keys periodically from the
  Massive dashboard; update `.env` and restart the WS job afterwards.
- Keys are only ever sent to `api.polygon.io`, `delayed.massive.com` and
  `files.massive.com` over TLS.

## Development

```
venv/bin/pip install pytest
venv/bin/python -m pytest tests/        # offline, fixture-based
venv/bin/python -m compileall ingest    # syntax check
```
