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
  What remains: every `vol_surface` partition landed before this was built
  unpinned, so the archive is only reproducible from the rebuild onward —
  the staging rebuild in `docs/plans/trading-day-calendar.md` decision 5 is
  what regenerates it under a known thread count. Recording the pinned value
  with the rows would also be needed for reproducibility to survive a hardware
  change; not done.
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
- `ingest/jobs/ws_minute_bars.py:648` — a 0-row capture pings healthcheck
  `/fail` but `main` still exits 0. Exit code and monitoring disagree; decide
  whether cron mail or Healthchecks is the alert channel, then align them.
- `ingest/jobs/eod_dayaggs_rest.py:91` — non-watchlist mode does one sequential
  REST call per contract (~100k contracts ≈ 6 h) with no checkpointing; a crash
  at hour 5 restarts from zero. Options: batch via a snapshot endpoint, or
  persist partial progress. Needs a runtime-budget decision.
- `ingest/jobs/grouped_daily.py:76` — an empty `records` on a trading day logs
  `grouped_empty` and exits 0 (green healthcheck). Consider failing when the
  response has results but none of the wanted tickers matched.
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
- `ingest/jobs/ws_minute_bars.py:117` — `contract_universe` uses bare
  `startswith(("O:SPY", "O:SPX"))`, which would admit `O:SPXL`/`O:SPXU` roots.
  Impossible with today's contracts partition; reuse the anchored regex from
  `keep_ticker` if the universe ever widens.

## Monitoring gaps

- `deploy/schedule.json` (`prune` entry) — **fixed**: the monthly prune job
  has a `healthchecks` block (`15 3 1 * *`, 180 min grace). `cronjob.sh`
  pings `/start` and success/`/fail` for `bash *.sh` commands, so the
  crontab fallback and the systemd unit share one definition. The generated
  unit now also gets `OnFailure=massive-alert@massive-prune`. Re-run
  `scripts/setup_healthchecks.py` on the box so the check is created with
  the monthly schedule; an auto-created check would default to daily.
- `pricing/drift_check.py:811` — `date.fromisoformat(args.date)` runs before
  the logger and `/start` ping, so a malformed `--date` dies with a bare
  traceback and no Healthchecks signal. Decide: argparse `type=` validation
  (exit 2 to cron mail) or logging against a fallback date.
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

- `pricing/engine.py:_bump_greeks` — ~45 CRR tree evaluations per `greeks()`
  call (each higher-order greek re-bumps from scratch). A shared-bump refactor
  could cut the drift canary's dominant cost roughly in half; too invasive for
  the audit.
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

- `ingest/common/market_gate.py:36` — the holiday cache is keyed by path with
  no mtime check; a session-long process keeps a stale calendar if
  `holidays_sync` rewrites the file mid-run. Fail-open by design, so low
  urgency; add mtime invalidation.
- `ingest/common/landing.py:212` — `quarantine_prior` uses `Path.replace`,
  overwriting a same-named quarantined file. Rare; collision-nudge the target.
- `ingest/jobs/coverage_audit.py:120` + `deploy/crontab:57` — on 13:00
  early-close days the cron cadence still runs to 16:30, so ~178 post-close
  sweeps read as "stray" and the 13:32–16:30 window is unchecked. Decide which
  side owns early closes: crontab stops early, or the audit treats the full
  window as canonical.
- `ingest/jobs/coverage_audit.py:536` / `reconcile.py:138` — default T-1 is
  computed without `data_root` (unlike `history_audit`), so a non-standard
  `DATA_ROOT` picks T-1 against the wrong holiday calendar. Pass the settings
  root consistently.
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
- `tests/conftest.py:93` — the offline guard patches `socket.connect` but not
  `connect_ex`, and would falsely reject AF_UNIX string addresses. Block
  `connect_ex` too and exempt non-IP addresses.
- `ingest/common/http_client.py` — `paginate` has no guard against a
  pathological repeated `next_url` (infinite loop); `cli.ping` truncates to
  10,000 *chars* before UTF-8 encoding, so a non-ASCII body can exceed the
  Healthchecks 10 KB limit.

## Docs / site

- `docs/404.html` uses relative asset paths; if it's ever served as a
  server-level 404 for deep URLs, switch to root-relative paths or a `<base>`
  tag depending on hosting.
- The "deja" image for the 404 page is not in the repo yet — drop it at
  `docs/assets/deja.png` (or `.jpg`); the page auto-enhances via an `onerror`
  fallback and looks complete without it.
- `.env.example` doesn't mention `TZ_NAME` or `TRADES_CONCURRENCY` (optional,
  sane defaults) — add commented entries.

## Environment / tooling

- `tests/test_prune_raw.py` requires GNU coreutils (`date -d`, `du -sb`) and
  bash 4 (`mapfile`); it fails on a stock macOS dev box and passes on Ubuntu
  CI. Either gate the test on `gdate`/`gdu` availability or document that
  `brew install coreutils bash` is needed for local runs.
