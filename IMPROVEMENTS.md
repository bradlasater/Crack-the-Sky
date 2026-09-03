# Improvements backlog

Findings from the 2026-09-02 full-repo audit that were **deliberately not fixed** —
each needs an owner decision, spans module boundaries, or is too invasive for a
conservative audit pass. Grouped by area, roughly highest-value first.

## Correctness / silent-failure risks

- `ingest/common/cli.py:205` — a job whose summary dict contains a reserved key
  (`rows`, `bytes`, `job`, `duration_s`) crashes `job_end` logging *after*
  succeeding, turning a good run into `job_error` + a `/fail` ping. Latent
  today. Fix: filter reserved keys from the `**extras` merge.
- `ingest/jobs/ws_minute_bars.py:612` — a 0-row capture pings healthcheck
  `/fail` but `main` still exits 0. Exit code and monitoring disagree; decide
  whether cron mail or Healthchecks is the alert channel, then align them.
- `ingest/jobs/eod_dayaggs_rest.py:91` — non-watchlist mode does one sequential
  REST call per contract (~100k contracts ≈ 6 h) with no checkpointing; a crash
  at hour 5 restarts from zero. Options: batch via a snapshot endpoint, or
  persist partial progress. Needs a runtime-budget decision.
- `ingest/jobs/grouped_daily.py:76` — an empty `records` on a trading day logs
  `grouped_empty` and exits 0 (green healthcheck). Consider failing when the
  response has results but none of the wanted tickers matched.
- `scripts/cronjob.sh:29` — exit-code collision: if the wrapped command exits
  99 it is misreported as `job_skipped` and exits 0. Latent (jobs only exit
  0/1/2/3 today). Use a rarer code or a lock-taken marker file.
- `marketdata/types.py:96` — `Quote.asof_ns` collapses three timestamps into
  one (underlying stamp first). `market_price` (last trade) can be far staler
  than the asof implies. Carry both stamps or document the choice.
- `marketdata/opra.py:106` vs `ingest/jobs/__init__.py:85` — two OPRA year-pivot
  decoders disagree on `yy >= 80` (19xx vs 20xx). Unreachable today; hoist one
  shared decoder before the universe widens.
- `ingest/jobs/ws_minute_bars.py:117` — `contract_universe` uses bare
  `startswith(("O:SPY", "O:SPX"))`, which would admit `O:SPXL`/`O:SPXU` roots.
  Impossible with today's contracts partition; reuse the anchored regex from
  `keep_ticker` if the universe ever widens.

## Monitoring gaps

- `deploy/crontab:111` + `scripts/setup_healthchecks.py:48` — the monthly
  `prune` job is unmonitored: no Healthchecks check exists and the drift test
  doesn't see it. The one job that deletes data could silently stop running.
  Add curl pings to `prune_raw.sh` (or ping support in `cronjob.sh`), register
  "prune" in `JOBS`, extend the drift-test regex.
- `pricing/drift_check.py:801` — `date.fromisoformat(args.date)` runs before
  the logger and `/start` ping, so a malformed `--date` dies with a bare
  traceback and no Healthchecks signal. Decide: argparse `type=` validation
  (exit 2 to cron mail) or logging against a fallback date.
- `ingest/jobs/snapshot_sweep.py:149` — one failing chain (e.g. VIX 403s) fails
  the whole run; the in-process retry re-fetches SPY+SPX and lands duplicate
  per-underlying files. Land per-chain successes and fail only if all chains
  fail, like `trades_watchlist` does.

## Performance

- `pricing/from_market.py:569` + `ingest/common/rates.py:103` — `resolve_r`
  re-reads every `treasury_yields` partition (history back to 1962) per quote.
  Memoize `load_curve` per `(date, data_root)` or hoist the curve into the
  chain loop, as `term_structure.build_for_date` already does.
- `pricing/engine.py:_bump_greeks` — ~45 CRR tree evaluations per `greeks()`
  call (each higher-order greek re-bumps from scratch). A shared-bump refactor
  could cut the drift canary's dominant cost roughly in half; too invasive for
  the audit.
- `ingest/jobs/contracts_sync.py:55` — first-ever run for a new underlying
  reads every historical partition to compute an empty baseline. Short-circuit
  via `catalog.files_by_underlying` name parsing.
- `ingest/jobs/trades_watchlist.py:161` — `trades_cursor.json` grows
  unboundedly; tickers that rotate off the watchlist keep cursors forever.
  Prune to the current watchlist at save time.
- `scripts/backfill.sh` — date payloads can run independently, but each process
  currently performs an unlocked read-modify-write of the shared
  `_meta/flatfile_manifest.json`; make manifest updates concurrency-safe first,
  then use a 4–8-way parallel backfill (e.g. `xargs -P`, staying inside the
  S3/rate budget) to reduce multi-year backfill wall time.
  day-to-day ingest is vendor-rate-bound (40 rps shared bucket), not
  compute-bound — parallelism only pays for backfills, not the live jobs.

## Robustness / consistency

- `ingest/common/market_gate.py:36` — the holiday cache is keyed by path with
  no mtime check; a session-long process keeps a stale calendar if
  `holidays_sync` rewrites the file mid-run. Fail-open by design, so low
  urgency; add mtime invalidation.
- `ingest/common/landing.py:212` — `quarantine_prior` uses `Path.replace`,
  overwriting a same-named quarantined file. Rare; collision-nudge the target.
- `ingest/jobs/coverage_audit.py:120` + `deploy/crontab:46` — on 13:00
  early-close days the cron cadence still runs to 16:30, so ~178 post-close
  sweeps read as "stray" and the 13:32–16:30 window is unchecked. Decide which
  side owns early closes: crontab stops early, or the audit treats the full
  window as canonical.
- `ingest/jobs/coverage_audit.py:536` / `reconcile.py:138` — default T-1 is
  computed without `data_root` (unlike `history_audit`), so a non-standard
  `DATA_ROOT` picks T-1 against the wrong holiday calendar. Pass the settings
  root consistently.
- `ingest/jobs/history_audit.py:280` — hand-rolled argv parser doesn't accept
  `--start=X`/`--end=X` equals-forms (dies loudly, not silently). Extend the
  loop or register the flags via the shared parser.
- `scripts/backfill.sh:60` vs `scripts/prune_raw.sh:98` — inconsistent
  "is this date done" semantics: backfill skips dates with ≥3 manifest entries
  regardless of `rows_kept`, so a 0-rows-kept date is skipped forever. Build a
  rows_kept-aware index in backfill like prune does.
- `tests/conftest.py:93` — the offline guard patches `socket.connect` but not
  `connect_ex`, and would falsely reject AF_UNIX string addresses. Block
  `connect_ex` too and exempt non-IP addresses.
- `ingest/common/http_client.py` — `paginate` has no guard against a
  pathological repeated `next_url` (infinite loop); `cli.ping` truncates to
  10,000 *chars* before UTF-8 encoding, so a non-ASCII body can exceed the
  Healthchecks 10 KB limit.

## Docs / site

- `pricing/from_market.py:146` — `expiry_instant` docstring omits VIX/VIXW
  settlement conventions.
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
