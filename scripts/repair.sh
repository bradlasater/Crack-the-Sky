#!/usr/bin/env bash
# repair.sh DATE — re-pull and re-verify one trading day.
#
# The standard fix when coverage_audit reports a gap: re-pull the flat files
# (authoritative), rewrite the clean minute-bar partition from them, then
# re-run the audit to confirm the hole is closed.
#
#   bash scripts/repair.sh 2026-08-28
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -ne 1 ]; then
    echo "usage: $0 DATE   (YYYY-MM-DD)" >&2
    exit 2
fi
D="$1"
PY="venv/bin/python"

echo "[repair] $D  1/3 flatfile_pull"
"$PY" -m ingest.jobs.flatfile_pull --date "$D"
echo "[repair] $D  2/3 reconcile"
"$PY" -m ingest.jobs.reconcile --date "$D"
echo "[repair] $D  3/3 coverage_audit"
if "$PY" -m ingest.jobs.coverage_audit --date "$D"; then
    echo "[repair] $D OK"
else
    echo "[repair] $D still has gaps — see the table above." >&2
    echo "[repair] snapshots cannot be repaired: they are only capturable live." >&2
    exit 1
fi
