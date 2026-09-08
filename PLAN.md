# Crack the Sky — build plan

Maps the target architecture (`preview.html`, the 16-box diagram dated
2026-09-06) onto what this repo actually contains, and sequences the next
few weeks of work. Companion docs:

- `docs/not-built.html` — the absence ledger (what is *not* in the tree).
- `docs/latent.html` + `IMPROVEMENTS.md` — presence with known failure modes;
  the fix backlog, not the build backlog. This plan is the build backlog.
- `preview.html` — the target. Open in a browser; it is the source of the
  box names used below.

Status legend: **built** (runs today) · **partial** (something real exists,
the box's job is not done) · **missing** (no code) · **blocked** (needs an
entitlement or owner decision before code helps).

## Box-by-box status

### Inputs

| Diagram box | Status | Reality |
|---|---|---|
| Live market data (real-time, for execution & live risk) | **blocked** | Plan tier is delayed: 1-min snapshot sweeps + delayed WS bars. A real-time quote feed is a vendor-plan decision, and it gates the whole execution column. |
| Delayed / research feeds | **built** | `ingest/` — snapshot sweeps, WS minute bars, trades watchlist, S3 flat files (authoritative, reconciled). |
| Broker / account inputs | **partial** | `ibkr_executions` (read-only Flex) ingests fills as a *dataset*. No positions, cash, margin, and no TWS/Gateway API — the diagram wants live account *state*. |

### Data foundation

| Diagram box | Status | Reality |
|---|---|---|
| Ingestion / capture | **built** | systemd timers via `deploy/`, typed reads in `marketdata/`, immutable parquet raw. |
| Validation / QC gate | **partial** | Fail-loud schema validation (`marketdata/validate.py`), `coverage_audit`, flat-file reconciliation. Missing: *market-level* QC — staleness, crossed markets, bad prints — and a gate that blocks downstream consumers. |
| Warehouse / event store | **partial** | Parquet raw/clean under `DATA_ROOT`. Append-only `decision_log` schema is in `ingest.schemas`; `quarantine_prior` and `read_asof` refuse it so last-file-wins cannot hide events. No live writer yet — the backtester (week 2) lands first. |
| Reference data | **built** | Contracts (incl. expired), dividends, rates (Treasury to 1962), holidays. Trading-day calendar is consumed on the day-bar path (hybrid vol time, stamped per row); the live path is still ACT/365. |

### Analytics / decision

| Diagram box | Status | Reality |
|---|---|---|
| Feature engineering | **partial** | ATM term structure (`pricing/term_structure.py`), SVI slice params (`pricing/surface.py`), continuous spot series (`signals/spot.py`). No RV features, no feature pipeline, no regimes/event flags. |
| Forecasting (HAR-RV baseline, distributions) | **missing** | The next real build item. `signals/spot.py` was written expressly to feed it (Stage 1.1 Roll-debias decision already measured: do not debias). |
| Pricing / surface analytics | **built** | Own IV (European + American/CRR), parity forwards, raw-SVI surface with butterfly *and* calendar arbitrage repaired inside the fit (`SurfaceArbitrageError` otherwise). Gaps on record: SPY smile (American under a European fit path), VIX surface, **no scheduled surface job**, drift_check's off-ATM consumer. |
| Signal / strategy engine | **missing** | No candidate structures, no entry/exit/roll/no-trade rules, no edge-net-of-costs. |

### Trading control loop — all missing, none near-term

| Diagram box | Status | Reality |
|---|---|---|
| Portfolio construction & risk | **missing** | Nothing. |
| Order management / execution | **missing + blocked** | No OMS; also needs the live feed and a broker API that don't exist yet. |
| Portfolio state / ledger / reconciliation | **missing** | `reconcile.py` reconciles *data* against flat files, not book-vs-broker. |
| Monitoring / watchdog / kill switches | **partial** | Ops monitoring is genuinely strong (per-job Healthchecks, drift canary, coverage audit, red-day playbook). Trading kill switches (risk breach → block/flatten) are absent — they have nothing to guard yet. |

### Research / governance loop — all missing

| Diagram box | Status | Reality |
|---|---|---|
| Backtesting / event replay | **missing** | No replay engine. The site's public credibility claim (pre-registered evaluation, walk-forward, purged validation) currently rests on tooling that does not exist. |
| Model evaluation / attribution | **missing** | Nothing. |
| Research sandbox | **partial** | `scripts/build_*.py` + the docs habit function as one, informally. |
| Governance / audit / dashboards | **partial** | Handbook docs + Healthchecks dashboards. Decision-log schema exists (append-only); no model versions or run reports, and nothing writes rows yet. |

## Scope discrepancies to resolve (owner decisions)

1. **Instruments.** The site says "SPX and XSP at 7–45 DTE"; the repo is
   SPY + SPX/SPXW + VIX options, and the term-structure code talks about a
   5–45 DTE book. XSP appears nowhere in the tree. Pick the book and make
   the site, the diagram, and the code agree.
2. **Live data.** Delayed-only is fine through backtesting and paper
   trading. It is a hard wall in front of the execution column. Decide when
   (not if) the plan tier upgrades, and let that date set the OMS schedule.
3. **IBKR API.** Flex gives yesterday's fills. Ledger, reconciliation, and
   any execution need TWS/Gateway access — an account/security decision,
   not a code decision.
4. **Trading-day calendar.** `_meta/trading_days.json` is built and
   verified; adopting it changes every T and theta in `pricing/`. Do it
   before the forecast and backtester bake ACT/365 deeper in.

## Next two weeks

Anchored to the diagram's own implementation order; the repo sits at the
end of its step 2. Roughly in sequence — each item lands runnable, with a
job or a script, not just a module.

**Week 1 — finish the analytics foundation:**

1. **Scheduled surface build.** `vol_surface` rebuilds exist only by hand
   (`scripts/build_surface.py`). Add the scheduled job + healthcheck, per
   the not-built ledger. Then wire drift_check's off-ATM slice consumer.
2. **Adopt the trading-day calendar** in `pricing/` (decision 4 above).
   Day-bar path: hybrid default, per-row stamp (step 3). Live path still
   ACT/365 (step 4). Production `clean/` swap is the remaining cutover.
3. **Decision-log schema.** Landed: append-only `decision_log` in
   `ingest.schemas`, writer `ingest.common.decision_log.write_decisions`.
   The backtester in week 2 writes first; nothing overwrites history
   (`quarantine_prior` / `read_asof` refuse the dataset).
4. **Baseline RV forecast.** HAR-RV on the `signals/spot` series,
   horizon-matched to the 5–45 DTE book, with an uncertainty band — the
   diagram is explicit: a distribution, not a point estimate. Honor the
   measured Stage 1.1 call (no Roll debias).

**Week 2 — close the loop on paper:**

5. **Feature generation v0.** Per (date, expiry): realized vol, ATM level
   and slope, SVI skew/curvature params, and the VRP spread
   (implied² − forecast²). Point-in-time only — every feature computable
   from data available that morning.
6. **Strategy engine skeleton.** One defined-risk structure. Explicit
   entry, exit, roll, and no-trade rules; expected edge net of spread cost
   from observed quotes, not assumed fills. Paper-only output into the
   decision log from item 3.
7. **Event replay v0.** Re-run the *same* strategy code over the day-bar
   archive (back to 2022-08-31), walk-forward, no lookahead. This is the
   first artifact that can validate or kill a hypothesis — build it so its
   outputs land in the same decision-log schema as live paper decisions.

**Explicitly not these two weeks:** anything in the trading-control column
(risk engine, OMS, ledger, kill switches). All are blocked on decisions
1–3 and none is load-bearing for proving edge. Also keep one eye on
`IMPROVEMENTS.md` — several latent items (cronjob exit-99 collision,
`snapshot_sweep` partial-chain failure, unmonitored `prune`) are cheap
fixes worth interleaving when a build item is soaking.
