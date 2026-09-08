#!/usr/bin/env bash
# backfill.sh START_DATE END_DATE — loop flatfile_pull over a date range.
#
# Resume-safe: a date is "done" once the manifest records all three datasets
# for it with rows_kept > 0 (the same rows_kept-aware index prune_raw.sh
# builds), so this can be killed and restarted freely. A date whose entries
# all parsed to zero rows is NOT done and is re-pulled, not skipped forever.
# Weekends/holidays are skipped by the job's market gate (quiet exit 0).
# Dates outside the plan's history window log flatfile_not_entitled and are
# skipped, not fatal.
#
# Runs NEWEST-FIRST by default. If the run is interrupted you keep the recent
# history, which is what a 5-45 day horizon model actually needs.
#
# Dates are independent, so the pull runs BACKFILL_WORKERS-wide in parallel
# (default 4; 1 restores the old serial loop). Manifest writes are
# flock-serialized inside flatfile_pull, so concurrent workers cannot corrupt
# _meta/flatfile_manifest.json. Parallelism belongs here, not in the live
# jobs: day-to-day ingest is vendor-rate-bound (40 rps shared bucket), only
# backfills are compute/wait-bound.
#
#   bash scripts/backfill.sh 2022-08-15 2026-08-27
#   MIN_FREE_GB=150 BACKFILL_ORDER=oldest BACKFILL_WORKERS=8 bash scripts/backfill.sh ...
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

PY="${BACKFILL_PY:-venv/bin/python}"
SLEEP_BETWEEN_DAYS="${BACKFILL_SLEEP_S:-2}"
MIN_FREE_GB="${MIN_FREE_GB:-100}"
ORDER="${BACKFILL_ORDER:-newest}"
WORKERS="${BACKFILL_WORKERS:-4}"
case "$WORKERS" in
    ''|*[!0-9]*|0)
        echo "[backfill] BACKFILL_WORKERS must be a positive integer (got '$WORKERS')" >&2
        exit 2
        ;;
esac
# Catches numerically-zero spellings the pattern misses ('00'); xargs -P 0
# would mean *unlimited* parallelism, the opposite of a rejected zero.
if [ "$WORKERS" -lt 1 ]; then
    echo "[backfill] BACKFILL_WORKERS must be a positive integer (got '$WORKERS')" >&2
    exit 2
fi

DATA_ROOT="${DATA_ROOT:-$(grep -E '^DATA_ROOT=' .env 2>/dev/null | cut -d= -f2 || true)}"
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

# The manifest is read ONCE into a rows_kept-aware index, exactly as
# prune_raw.sh builds it: dataset|date, kept only when rows_kept > 0. The old
# check counted entries regardless of rows_kept, so a date whose three files
# parsed to zero SPY/SPX rows was treated as done and skipped forever.
MANIFEST_INDEX="$(mktemp)"
trap 'rm -f "$MANIFEST_INDEX"' EXIT
if [ -f "$MANIFEST" ]; then
    python3 - "$MANIFEST" > "$MANIFEST_INDEX" <<'PY'
import json, sys
try:
    rows = json.load(open(sys.argv[1]))
except Exception:
    rows = []
seen = set()
for e in rows:
    if not isinstance(e, dict):
        continue
    if (e.get("rows_kept") or 0) > 0 and e.get("dataset") and e.get("date"):
        seen.add(f"{e['dataset']}|{e['date']}")
print("\n".join(sorted(seen)))
PY
fi

manifest_has_date() {
    # True when the index has all 3 datasets for $1 with rows actually kept.
    [ -s "$MANIFEST_INDEX" ] || return 1
    for ds in trades_v1 minute_aggs_v1 day_aggs_v1; do
        grep -qxF "$ds|$1" "$MANIFEST_INDEX" || return 1
    done
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

# Filter out dates already done; the manifest is only appended to during the
# run, so a date skipped here cannot become unfinished mid-run.
todo=()
for d in ${dates[@]+"${dates[@]}"}; do
    manifest_has_date "$d" || todo+=("$d")
done

total=${#dates[@]}
avail="$(free_gb)"; avail="${avail:-0}"
echo "[backfill] $total dates, $ORDER-first, $((total - ${#todo[@]})) already done, ${#todo[@]} to pull, workers $WORKERS, min free ${MIN_FREE_GB}GB, ${avail}GB available"

if [ "${#todo[@]}" -eq 0 ]; then
    echo "[backfill] done ($START .. $END, $ORDER-first)"
    exit 0
fi

run_one() {
    # Pull one date. A per-date failure is logged and swallowed so one bad
    # date never sinks the batch; only the low-disk abort propagates (255).
    d="$1"
    avail="$(free_gb)"; avail="${avail:-0}"
    if [ "$avail" -lt "$MIN_FREE_GB" ]; then
        echo "[backfill] ABORT: only ${avail}GB free on $DATA_ROOT (min ${MIN_FREE_GB}GB)" >&2
        echo "[backfill] resume with the same command once space is reclaimed" >&2
        return 255
    fi
    echo "[backfill] $d  [${avail}GB free]"
    "$PY" -m ingest.jobs.flatfile_pull --date "$d" || \
        echo "[backfill] $d flatfile_pull exited $? — continuing"
    sleep "$SLEEP_BETWEEN_DAYS"
}

if [ "$WORKERS" -eq 1 ]; then
    i=0
    for d in ${todo[@]+"${todo[@]}"}; do
        i=$((i + 1))
        echo "[backfill] ($i/${#todo[@]})"
        run_one "$d" || exit 1
    done
else
    export -f run_one free_gb
    export DATA_ROOT MIN_FREE_GB PY SLEEP_BETWEEN_DAYS
    rc=0
    # A worker's 255 (low disk) makes GNU xargs stop launching new dates and
    # exit nonzero; pulls already in flight still finish their current date
    # (bounded work) and free space is re-checked before every launch.
    printf '%s\n' "${todo[@]}" | xargs -r -P "$WORKERS" -n 1 bash -c 'run_one "$1"' _ || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "[backfill] aborted (worker exit $rc); resume with the same command" >&2
        exit 1
    fi
fi
echo "[backfill] done ($START .. $END, $ORDER-first)"
