# Improvements backlog

Findings from the 2026-09-02 full-repo audit that were **deliberately not fixed** —
each needs an owner decision, spans module boundaries, or is too invasive for a
conservative audit pass. Grouped by area, roughly highest-value first.

## Correctness / silent-failure risks

- `pricing/surface.py` (whole module) — **the landed SVI archive is not
  reproducible.** Rebuilding one session with identical code and inputs but a
  different BLAS thread count returns different parameters: measured on
  2026-09-04, 1 thread vs 8 moved 397 of 720 values, `svi_rho` by up to
  2.24e-03 relative and `svi_a`/`svi_m` by ~2e-05, while `rms_error` moved by
  <1e-09 — the optimiser lands elsewhere in a flat basin, so the fit is equally
  good and the parameters are not the same numbers. `atm_term_structure` is
  unaffected (scalar Brent inversion, no BLAS). Consequences: a rebuild cannot
  be diffed against the existing archive to measure a deliberate change (the
  convention change in `docs/plans/trading-day-calendar.md` hits this
  directly), and the backtester cannot reproduce the inputs a decision was made
  on. **Pinned going forward** in `scripts/cronjob.sh` (every scheduled job,
  so the systemd timers and the crontab fallback share one definition) and in
  `scripts/build_surface.py`, which is run directly and must set it above the
  `pricing` import because OpenBLAS reads its thread count once, at load.
  **Recorded with the rows**: `vol_surface` gained a `blas_threads` column
  stamped with the pinned count on every row (null when a run was unpinned;
  a deliberate operator override is stamped as-is), so reproducibility no
  longer depends on remembering what the box was. `atm_term_structure` is not
  stamped — no BLAS underneath, so there is no count to record. What remains:
  the schema is fail-loud on a missing column, so the stamp only exists on
  partitions written by the new code — the staging rebuild in
  `docs/plans/trading-day-calendar.md` decision 5 ("Second pass") regenerates
  the archive under the known thread count and the swap makes it readable
  again. Until then the archive is reproducible only from the day the pin
  landed, and production `vol_surface` raises under the new code.
- `pricing/from_market.py:200` — `expiry_instant` and `year_fraction` accept a
  session calendar that moves a PM-settled expiry to the 13:00 ET early close,
  but **no production caller passes one yet**, so the default is still the
  root's nominal 16:00. On the two half days in the current window — Black
  Friday 2026-11-27 and Christmas Eve 2026-12-24 — every SPY/SPXW contract
  expiring that day therefore carries three hours of vol time that was never
  traded, and prices as live for three hours after it has settled. AM-settled
  roots are unaffected: an early close moves the close, not the open. Wiring
  the live path is step 4 of `docs/plans/trading-day-calendar.md`; the first
  date it actually bites is 2026-11-27.
- `ingest/common/cli.py` — **fixed**: reserved keys (`event`, `rows`, `bytes`,
  `job`, `duration_s`) are dropped from the `**extras` merge so a successful
  job cannot crash `job_end` and get reported as `job_error`.
- `ingest/jobs/ws_minute_bars.py:648` — **fixed**: Healthchecks is the alert
  channel and the exit code now agrees with it. A capture that ends with zero
  rows, or one that lost records to writer errors, pings `/fail` and returns
  1. Legitimate zero-row situations stay green: a holiday still exits 0 from
  the market gate, and an already-closed capture window returns 0 before any
  capture runs.
- `ingest/jobs/eod_dayaggs_rest.py:91` — **fixed**: partial progress is
  persisted (endpoint strategy unchanged). The full-universe sweep
  checkpoints done tickers and counters to `_meta/dayaggs_checkpoint.json`
  and appends fetched raw bars to `_meta/dayaggs_partial.jsonl` every 500
  contracts, so a restarted run for the same date resumes where it stopped;
  both files are removed on completion. Missing/corrupt/stale-date state
  restarts the sweep — refetching is idempotent, skipping would be a silent
  gap. The watchlist sweep and dry runs do not checkpoint.
- `ingest/jobs/grouped_daily.py:76` — **fixed**: when the grouped response
  has market rows but none of the wanted tickers are among them, the job
  raises (nonzero exit, `/fail` ping) instead of logging `grouped_empty` and
  exiting 0. A genuinely empty response (forced holiday run, wrong date)
  still lands `grouped_empty` and stays green; real holidays never reach
  `main_fn` because the market gate exits 0 first.
- `scripts/cronjob.sh` — **fixed**: the lock is taken on fd 9 before the
  command runs, so a wrapped process that exits 99 is no longer misreported
  as `job_skipped` and swallowed to 0. Contention is flock's `-E 99` on
  that fd; a missing `flock` or an unusable lock file stays nonzero.
- `marketdata/opra.py:106` vs `ingest/jobs/__init__.py:85` — **fixed**: one
  shared decoder, `expiry_year()` in `marketdata/opra.py`, used by both
  `parse_opra` and `ingest.jobs.parse_option_ticker`. The convention chosen is
  `2000 + yy` with no 19xx pivot: everything the codebase reads (vendor flat
  files from 2020 on, the live-universe REST reference, four years of
  `option_day_bars` history) holds only 21st-century contracts, and the
  ingest-side parser already decoded that way, so the term-structure archive
  built on its output stays valid. A `yy >= 80` suffix is a corrupt ticker,
  and 2080+ is a less dangerous decode than an expiry decades in the past.
- `ingest/jobs/ws_minute_bars.py:117` — **fixed**: `contract_universe` matches
  roots with an anchored OPRA regex (`^O:(ROOT)\d{6}[CP]\d+$`, the same shape
  as `keep_ticker`'s), so `O:SPXL`/`O:SPXU` can never be admitted. Weekly
  roots ride with their underlying (`SPX` also admits `SPXW`).

## Monitoring gaps

- `deploy/schedule.json` (`prune` entry) — **fixed**: the monthly prune job
  has a `healthchecks` block (`15 3 1 * *`, 180 min grace). `cronjob.sh`
  pings `/start` and success/`/fail` for `bash *.sh` commands, so the
  crontab fallback and the systemd unit share one definition. The generated
  unit now also gets `OnFailure=massive-alert@massive-prune`. Re-run
  `scripts/setup_healthchecks.py` on the box so the check is created with
  the monthly schedule; an auto-created check would default to daily.
- `pricing/drift_check.py:811` — **fixed**: `--date` now validates through an
  argparse `type=` converter, so a malformed date exits 2 with a usage error
  on stderr (cron mail) before the logger or any ping exists. Chosen over
  logging against a fallback date: the run never starts, so there is nothing
  to log against, and exit 2 is distinct from the canary's exit-1 FAIL path.
- `ingest/jobs/snapshot_sweep.py` — **fixed**: chains now fail independently.
  One bad chain is recorded and the rest still land; the run fails only when
  every chain does. Previously a single failure discarded the chains that had
  already succeeded *and* had `run_job` retry the whole sweep, re-fetching them
  into a second parquet for the same minute — duplicate rows in the one dataset
  that cannot be backfilled. The monitoring half is closed too: because a
  partial failure has to report success, the job's own check would stay green
  for a chain that is down on every run, so a second check
  (`snapshot_sweep_all_chains`) is pinged *only* when every chain came back
  clean. Its grace window is the alert — one transient failure is absorbed by
  the next minute's clean sweep, and a chain down longer than the grace stops
  the pings and pages. Alerting by absence rather than by `/fail` is
  deliberate: at a 1-minute cadence, failing on any bad chain would page on
  every transient 429.

## Performance

- `pricing/engine.py:_bump_greeks` — **fixed**: the bumped CRR trees are now
  memoized by their exact argument tuple, so the higher-order greeks reuse the
  first-order bumps instead of re-pricing from scratch (43 → 28 tree
  evaluations per `greeks()` call, ~1.5× measured wall-time speedup). On one
  platform the outputs are bit-for-bit identical — `crr_price` is a pure
  function of its scalar inputs and every call site computes bumped arguments
  with the same expressions; pinned by
  `tests/pricing/test_bump_greeks_characterization.py` (golden `float.hex()`
  values compared at rel=1e-12, since the tree's exp/pow differ by a few ULPs
  across libm/Python builds — bit-exact goldens are not portable). The
  "roughly half" estimate was optimistic: 28 is the floor for the current
  finite-difference scheme; going lower would change the numerics.
- `ingest/jobs/contracts_sync.py:55` — **fixed**: `_previous_tickers` now
  answers "not here" from the filenames before opening any parquet. Clean
  files are named `{job}-{underlying}-{epoch_ms}.parquet`, so a partition
  with no file labelled with the underlying cannot hold a baseline for it
  (via `_latest_files_by_underlying`, which wraps
  `catalog.files_by_underlying`). A new underlying's first run no longer
  scans the whole archive to compute an empty set; files whose name carries
  no underlying label are still read, since their contents are not in the
  filename.
- `ingest/jobs/trades_watchlist.py:161` — **fixed**: cursors are pruned to the
  current watchlist at save time, in the same block that already pruned the
  backoff state (and with the same `--limit` exemption, since a truncated
  `tickers` list would wipe state for contracts the smoke test never looked
  at). A pruned contract that rotates back on re-polls its full history --
  duplicates, never gaps, because a cursor only moves forward and
  flat-file-covered days are dropped at write time. The `cursors_saved` event
  now logs the pruned count.
- `scripts/backfill.sh` — **fixed**: `_update_manifest` in
  `ingest/jobs/flatfile_pull.py` now serializes its read-modify-write with an
  `flock` on `_meta/flatfile_manifest.lock` and writes via temp-file rename
  (the SharedTokenBucket pattern), so concurrent flatfile_pull processes
  cannot corrupt or lose manifest entries. backfill.sh then runs dates
  `BACKFILL_WORKERS`-wide in parallel (`xargs -P`, default 4, 1 = the old
  serial loop); a low-disk worker exits 255, which stops xargs from launching
  further dates (in-flight pulls finish their current date, then the run
  aborts with the usual resume message).
  Day-to-day ingest stays serial: it is vendor-rate-bound (40 rps shared
  bucket), not compute-bound — parallelism only pays for backfills.

## Robustness / consistency

- `ingest/common/market_gate.py:36` — **fixed**: the holiday cache now carries
  the mtime it was read at and reloads when the file changes, so a
  session-long process picks up a mid-run `holidays_sync` rewrite. A missing
  file still caches as empty (fail-open) and starts answering the moment the
  file appears.
- `ingest/common/landing.py:212` — **fixed**: `quarantine_prior` now nudges
  the stamp token forward when the quarantine target name is already taken,
  so a second refilter/reconcile of one partition no longer overwrites the
  earlier quarantined file. Same shape-preserving nudge as
  `_unique_clean_path` (readers parse the final `-` token as an integer
  stamp).
- `ingest/jobs/coverage_audit.py:120` + `deploy/crontab:57` — **fixed**:
  owner decision was that the audit owns early closes, so the crontab stays
  as installed. The canonical sweep window already ended at the actual
  session close via `market_gate.market_close_et`; what misread was the
  classification — `_classify_stamps` now puts the post-close cadence firings
  (13:33–16:24 on a 13:00 close, up to the crontab's hard stop) in their own
  `post_close` bucket: accounted for in the check data, never counted towards
  the ratio, never required (a sweep job that learns to stop at the early
  close must not fail the day it ships), and no longer reported as ~178
  "stray" sweeps. Layering: the audit keeps reading `_meta/holidays.json`
  through `market_gate` rather than importing `pricing.calendar` — the repo's
  import direction is pricing → ingest (`pricing.calendar` itself unions
  those same files via `market_gate`), and ingesting pricing would invert it
  for zero new information.
- `ingest/jobs/coverage_audit.py:536` / `reconcile.py:138` — **fixed**: both
  jobs now compute the T-1 default against `config.default_data_root()`, a
  new helper that resolves `DATA_ROOT` exactly as `Settings.load()` does
  (environment, then .env) without the credential check. `main()` runs before
  `run_job`'s `Settings.load()`, so a `DATA_ROOT` that lives only in .env was
  previously invisible and T-1 was picked against `/data/massive`'s holiday
  calendar — the same root `history_audit` already passes explicitly.
- `ingest/jobs/history_audit.py:280` — **fixed**: the hand-rolled loop now
  accepts `--start=X`/`--end=X` equals-forms alongside the space-separated
  ones. Done by extending the loop rather than the shared parser, matching
  the repo convention that jobs peel their own flags before handing the rest
  to `cli.run_job` (`strip_flag` documents the pattern); the shared parser
  only knows flags common to every job.
- `scripts/backfill.sh:60` vs `scripts/prune_raw.sh:98` — **fixed**: backfill
  now builds the same rows_kept-aware manifest index prune does
  (`dataset|date`, kept only when `rows_kept > 0`) and calls a date done only
  when all three datasets have rows kept, so a 0-rows-kept date is re-pulled
  instead of skipped forever. Pinned by tests/test_backfill.py.
- `tests/conftest.py:93` — **fixed**: the offline guard now patches
  `connect_ex` alongside `connect`, and the host check only applies to
  `AF_INET`/`AF_INET6` sockets, so AF_UNIX path addresses (local by
  construction) are no longer misread as outbound hosts. Covered by
  `tests/test_offline_guard.py`.
- `ingest/common/http_client.py` — **fixed**: `paginate` stops with a warning
  when the API re-serves a `next_url` it already followed (a stuck cursor
  would otherwise page forever), and `cli.ping` now truncates the body after
  UTF-8 encoding — 10,000 *bytes*, with any half-encoded tail character
  dropped — so a non-ASCII body can no longer exceed the Healthchecks 10 KB
  limit.

## Docs / site

- `docs/404.html` — **fixed**: when the page is served as a server-level 404
  for a deep URL (any http(s) request whose path is not 404.html itself), an
  inline script inserts `<base href="/Crack-the-Sky/">` before the stylesheet
  and image references, so they resolve against the site root instead of the
  missing directory. Root-relative paths were not an option — the drift tests
  pin the relative `href="site.css"` on every page, and GitHub Pages (not yet
  enabled; the user site redirects to the bradlasater.com custom domain, so
  the conventional mount is `/<repo>/`) would serve the handbook under a
  subpath. Opened directly as `docs/404.html` or via file://, no base is
  inserted and the relative paths work as before.
- The "deja" image for the 404 page is not in the repo yet — drop it at
  `docs/assets/deja.png` (or `.jpg`); the page auto-enhances via an `onerror`
  fallback and looks complete without it.
- `.env.example` — **fixed**: commented `TZ_NAME` (default
  `America/New_York`, the market-session gate's exchange clock in
  `ingest/common/market_gate.py`) and `TRADES_CONCURRENCY` (default 8,
  concurrent contract fetches in `trades_watchlist`) entries added. Both were
  already documented in `docs/knobs.html`.

## Environment / tooling

- `tests/test_prune_raw.py` — **fixed**: the module now probes the exact
  commands the script runs (`date -I -d`, `du -sb`) at collection time and
  skips the whole file with a `brew install coreutils` hint when they fail,
  so stock macOS dev boxes get skips instead of failures. (`mapfile` is no
  longer in `scripts/prune_raw.sh`, so bash 4 is not a separate gate.)
