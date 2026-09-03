#!/usr/bin/env bash
# backfill.sh START_DATE END_DATE — loop flatfile_pull over a date range.
#
# Resume-safe: dates already recorded in _meta/flatfile_manifest.json are
# skipped, so this can be killed and restarted freely. Weekends/holidays are
# skipped by the job's market gate (quiet exit 0). Dates outside the plan's
# history window log flatfile_not_entitled and are skipped, not fatal.
#
# Runs NEWEST-FIRST by default. If the run is interrupted you keep the recent
# history, which is what a 5-45 day horizon model actually needs.
#
#   bash scripts/backfill.sh 2022-08-15 2026-08-27
#   MIN_FREE_GB=150 BACKFILL_ORDER=oldest bash scripts/backfill.sh ...
#
# Do not run this in a terminal you will close:
#   systemd-run --user --unit=massive-backfill \
#       bash scripts/backfill.sh 2022-08-15 2026-08-27
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -ne 2 ]; then
    echo "usage: $0 START_DATE END_DATE   (YYYY-MM-DD)" >&2
    exit 2
fi
START="$1"; END="$2"

# Validate the original arguments before any clamping or comparison below:
# date -d accepts relative/noncanonical input ('today', '2026-1-1'), but the
# loop and the range check compare the given strings lexically, so require
# the parsed date to round-trip to the exact YYYY-MM-DD input.
for v in "$START" "$END"; do
    [ "$(date -I -d "$v" 2>/dev/null)" = "$v" ] || {
        echo "[backfill] invalid date: $v (want YYYY-MM-DD)" >&2; exit 2; }
done

PY="venv/bin/python"
SLEEP_BETWEEN_DAYS="${BACKFILL_SLEEP_S:-2}"
MIN_FREE_GB="${MIN_FREE_GB:-100}"
ORDER="${BACKFILL_ORDER:-newest}"

DATA_ROOT="$(grep -E '^DATA_ROOT=' .env 2>/dev/null | cut -d= -f2 || true)"
DATA_ROOT="${DATA_ROOT:-/data/massive}"
MANIFEST="$DATA_ROOT/_meta/flatfile_manifest.json"

# Earliest date each flat-file dataset exists in the bucket. Asking for
# anything earlier just burns requests on 403s.
#   trades_v1 2014, day_aggs_v1 2014, minute_aggs_v1 2022
EARLIEST="2022-01-01"
if [[ "$START" < "$EARLIEST" ]]; then
    echo "[backfill] START $START is before minute_aggs_v1 exists; clamping to $EARLIEST" >&2
    echo "[backfill] (trades_v1/day_aggs_v1 reach back to 2014 -- pull those separately if wanted)" >&2
    START="$EARLIEST"
fi

# Both dates were validated as canonical YYYY-MM-DD above, so a lexical
# comparison is exact. Catch a reversed range here instead of silently
# processing zero dates and reporting "done".
if [[ "$START" > "$END" ]]; then
    echo "[backfill] START $START is after END $END" >&2
    exit 2
fi

# An unreadable/missing DATA_ROOT reads as 0 free, so the loop aborts with
# the clear low-space message instead of dying on an empty string comparison.
free_gb() { df -BG --output=avail "$DATA_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9' || true; }

manifest_has_date() {
    # True when the manifest already has entries for all 3 datasets on $1.
    [ -f "$MANIFEST" ] || return 1
    [ "$(grep -c "\"date\": \"$1\"" "$MANIFEST" || true)" -ge 3 ]
}

# Build the date list in the requested order.
dates=()
d="$START"
while [[ ! "$d" > "$END" ]]; do
    dates+=("$d")
    d="$(date -I -d "$d + 1 day")"
done
if [ "$ORDER" = "newest" ]; then
    mapfile -t dates < <(printf '%s\n' "${dates[@]}" | sort -r)
fi

total=${#dates[@]}
avail="$(free_gb)"; avail="${avail:-0}"
echo "[backfill] $total dates, $ORDER-first, min free ${MIN_FREE_GB}GB, ${avail}GB available"

i=0
for d in "${dates[@]}"; do
    i=$((i + 1))
    avail="$(free_gb)"; avail="${avail:-0}"
    if [ "$avail" -lt "$MIN_FREE_GB" ]; then
        echo "[backfill] ABORT: only ${avail}GB free on $DATA_ROOT (min ${MIN_FREE_GB}GB)" >&2
        echo "[backfill] resume with the same command once space is reclaimed" >&2
        exit 1
    fi
    if manifest_has_date "$d"; then
        continue
    fi
    echo "[backfill] ($i/$total) $d  [${avail}GB free]"
    "$PY" -m ingest.jobs.flatfile_pull --date "$d" || \
        echo "[backfill] $d flatfile_pull exited $? — continuing"
    sleep "$SLEEP_BETWEEN_DAYS"
done
echo "[backfill] done ($START .. $END, $ORDER-first)"
