#!/usr/bin/env bash
# backfill.sh START_DATE END_DATE — loop flatfile_pull over a date range.
#
# Resume-safe: dates whose datasets are already recorded in
# _meta/flatfile_manifest.json are skipped. Weekends/holidays are skipped by
# the job's market gate (quiet exit 0). Sleeps politely between trading days.
#
#   bash scripts/backfill.sh 2026-08-01 2026-08-31
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -ne 2 ]; then
    echo "usage: $0 START_DATE END_DATE   (YYYY-MM-DD)" >&2
    exit 2
fi
START="$1"; END="$2"
PY="venv/bin/python"
SLEEP_BETWEEN_DAYS="${BACKFILL_SLEEP_S:-2}"

DATA_ROOT="$(grep -E '^DATA_ROOT=' .env 2>/dev/null | cut -d= -f2 || true)"
DATA_ROOT="${DATA_ROOT:-/data/massive}"
MANIFEST="$DATA_ROOT/_meta/flatfile_manifest.json"

manifest_has_date() {
    # True when the manifest already has entries for all 3 datasets on $1.
    [ -f "$MANIFEST" ] || return 1
    [ "$(grep -c "\"date\": \"$1\"" "$MANIFEST" || true)" -ge 3 ]
}

d="$START"
while [[ ! "$d" > "$END" ]]; do
    if manifest_has_date "$d"; then
        echo "[backfill] $d already in manifest — skipping"
    else
        echo "[backfill] $d pulling flat files"
        # market gate inside the job skips weekends/holidays quietly
        "$PY" -m ingest.jobs.flatfile_pull --date "$d" || \
            echo "[backfill] $d flatfile_pull exited $? — continuing"
        sleep "$SLEEP_BETWEEN_DAYS"
    fi
    d="$(date -I -d "$d + 1 day")"
done
echo "[backfill] done ($START .. $END)"
