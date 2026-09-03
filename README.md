# Crack the Sky

A personal market-data pipeline and options-pricing stack for SPY/SPX volatility
data, built on the Massive.com (ex-Polygon.io) "Options Developer" plan. It
captures option market state all day, stores it as parquet on a headless Linux
box, and re-prices the chain with its own IV/greeks engine — with a daily
canary that pages if the math stops agreeing with reality.

Python 3.11+, no pandas (`pyarrow`, `numpy`, `scipy`, `requests`, `websockets`,
`boto3`).

## What it captures

- **Full-chain option snapshots, every minute** — IV, greeks, and open interest
  for SPY and SPX/SPXW, plus underlying price for SPY (SPX spot is recovered
  from put-call parity). Historical snapshots cannot be reproduced, so they
  are swept live or lost forever.
- **Delayed option minute bars** over websocket during the session.
- **Trades** on a liquid watchlist, polled every 5 minutes.
- **S3 flat files** (trades / minute aggs / day aggs) pulled the next morning —
  the authoritative record; everything same-day is reconciled against them and
  the flat file always wins.
- **Reference data**: contracts (incl. expired), dividends, splits, holidays,
  the Treasury curve for discounting.
- **Broker executions** via IBKR's read-only Flex Web Service.

## How it fits together

```
Massive (WS / REST / S3) + IBKR ──cron──▶ ingest/ ──▶ /data/massive (parquet)
                                                         │
                                              marketdata/ (typed reads)
                                                         │
                                          pricing/ (own IV, BSM / CRR)
                                                         │
                                       drift_check canary → Healthchecks
```

Three packages: **`ingest/`** captures, **`marketdata/`** reads the warehouse
with fail-loud schema validation, **`pricing/`** computes. Vendor IV/greeks are
kept only as diagnostics — every number the system acts on is derived in-house.

## The one thing to understand

Two classes of data, opposite treatment:

| | Backfillable? | Consequence |
|---|---|---|
| `option_snapshots` | **No** | Swept every minute; everything else yields API budget to it. |
| trades, bars | **Yes** (S3 flat files) | A missed day is an inconvenience; re-pull it. |

## Operations

- Runs on an Ubuntu headless box; `cron` + `flock` is the whole scheduler
  (`deploy/crontab` is the source of truth). Every line goes through
  `scripts/cronjob.sh`, so a run dropped for overlapping the previous one
  logs `job_skipped` rather than vanishing.
- Every job has its own Healthchecks.io check — a dead job pages, it doesn't
  hide. A daily `coverage_audit` alarms on any capture gap, and on the disk
  runway left at the current rate of snapshot growth.
- Outbound REST rate is bounded per *box*, not per process, and every capture
  job except `snapshot_sweep` draws at low priority — the API budget yields to
  the dataset that cannot be re-pulled.
- CI is split on purpose: GitHub-hosted runs the offline test/lint gate on
  every PR; a self-hosted runner on the box runs live entitlement probes and
  the daily coverage audit against real credentials and data.

## Documentation

The system handbook lives in `docs/` — start at `docs/index.html` (dark,
multi-page, one file per section). Machine state the repo can't declare
(secrets, installed schedule, runner) is in `docs/box-operations.html`.

## Quickstart

On the box: `bash scripts/bootstrap.sh`, fill in `.env` from `.env.example`,
probe the plan with `venv/bin/python -m ingest.entitlements`, then install the
schedule — rewriting the placeholder home path first, since cron does not
expand `~`/`$HOME`:

```
sed "s|/home/brad-lasater|$HOME|g" deploy/crontab | crontab -
```

## Security

`.env` is gitignored — never commit it. All keys are read-only
market-data credentials, sent only to the vendor hosts over TLS.
