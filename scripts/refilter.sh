#!/usr/bin/env bash
# refilter.sh START END — re-filter landed flat files without re-downloading.
#
# For when the ticker allowlist widens (VIX/VIXW) or the parser is fixed
# (nanosecond timestamps): the raw .csv.gz is already on disk, so this reuses
# it after verifying the manifest md5 and rewrites only the clean parquet.
#
# Each date's previous flatfile_pull output is moved to _quarantine/refilter/
# rather than deleted, so a whole-partition read cannot double-count and the
# old output stays recoverable.
#
#   bash scripts/refilter.sh 2022-08-31 2026-08-31
#
# Resumable: re-running skips nothing, but it is idempotent -- a second pass
# quarantines the first pass's output and rewrites identical parquet.
set -euo pipefail
cd "$(dirname "$0")/.."

[ $# -eq 2 ] || { echo "usage: $0 START END   (YYYY-MM-DD)" >&2; exit 2; }
START="$1"; END="$2"
PY="venv/bin/python"
MIN_FREE_GB="${MIN_FREE_GB:-80}"
DATA_ROOT="$(grep -E '^DATA_ROOT=' .env 2>/dev/null | cut -d= -f2 || true)"
DATA_ROOT="${DATA_ROOT:-/data/massive}"

free_gb() { df -BG --output=avail "$DATA_ROOT" | tail -1 | tr -dc '0-9'; }

# Newest first: if this is interrupted, the recent history a 5-45 DTE model
# actually uses is already done.
dates=()
d="$START"
while [[ ! "$d" > "$END" ]]; do dates+=("$d"); d="$(date -I -d "$d + 1 day")"; done
mapfile -t dates < <(printf '%s\n' "${dates[@]}" | sort -r)

total=${#dates[@]}; i=0; done_n=0; skip_n=0
failed=()
echo "[refilter] $total dates, newest first, ${MIN_FREE_GB}GB floor, $(free_gb)GB free"
for d in "${dates[@]}"; do
    i=$((i+1))
    avail="$(free_gb)"
    if [ "$avail" -lt "$MIN_FREE_GB" ]; then
        echo "[refilter] ABORT: ${avail}GB free < ${MIN_FREE_GB}GB" >&2; exit 1
    fi
    # No raw file for this date (weekend/holiday, or never pulled) -> skip fast.
    if ! ls "$DATA_ROOT"/raw/flatfiles/trades_v1/dt="$d"/*.csv.gz >/dev/null 2>&1; then
        skip_n=$((skip_n+1)); continue
    fi
    rc=0
    "$PY" -m ingest.jobs.flatfile_pull --date "$d" --replace --force >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 0 ]; then
        done_n=$((done_n+1))
        [ $((done_n % 25)) -eq 0 ] && \
            echo "[refilter] ($i/$total) $d  done=$done_n skipped=$skip_n  [${avail}GB free]"
    else
        failed+=("$d")
        echo "[refilter] $d FAILED (exit $rc) — continuing" >&2
    fi
done

# A partial history rewrite must not look like a successful one. Later dates
# still run after a failure, but the exit status carries the failures out to
# whatever invoked this -- an operator who missed stderr, or cron.
if [ ${#failed[@]} -gt 0 ]; then
    echo "[refilter] INCOMPLETE: $done_n refiltered, $skip_n skipped, ${#failed[@]} FAILED" >&2
    printf '[refilter]   failed: %s\n' "${failed[@]}" >&2
    exit 1
fi
echo "[refilter] complete: $done_n refiltered, $skip_n skipped (no raw file)"
