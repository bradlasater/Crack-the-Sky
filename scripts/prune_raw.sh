#!/usr/bin/env bash
# Prune raw payloads that can be rebuilt, keeping everything that cannot.
#
# WHAT IS SAFE TO DELETE, AND WHY
#   raw/option_trades          rebuilt from the trades_v1 flat file
#   raw/option_day_bars        rebuilt from the day_aggs_v1 flat file
#   raw/option_minute_bars_ws  superseded at reconcile by minute_aggs_v1
#   raw/underlying_day_bars    one grouped-daily REST call rebuilds it
#
# WHAT IS NEVER TOUCHED
#   clean/**                   the ClickHouse source of truth
#   raw/option_snapshots       IV/greeks/OI cannot be rebuilt from anything
#   raw/flatfiles              the authoritative vendor payload
#   raw/contracts, dividends, splits, holidays   tiny reference data
#   _meta/**, logs/**
#
# A partition is only pruned when the flat file that replaces it is recorded
# in _meta/flatfile_manifest.json with rows kept, so a failed pull can never
# lead to deleting the only copy.
#
# Usage:  bash scripts/prune_raw.sh [--apply]     (dry-run without --apply)
#         RETAIN_DAYS=90 bash scripts/prune_raw.sh --apply
set -euo pipefail

DATA_ROOT="${DATA_ROOT:-/data/massive}"
RETAIN_DAYS="${RETAIN_DAYS:-90}"
MANIFEST="$DATA_ROOT/_meta/flatfile_manifest.json"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

# dataset:required flat-file dataset in the manifest ("-" = no requirement)
PRUNABLE=(
  "option_trades:trades_v1"
  "option_day_bars:day_aggs_v1"
  "option_minute_bars_ws:minute_aggs_v1"
  "underlying_day_bars:-"
)

log() { printf '{"ts":"%s","event":"prune","%s}\n' "$(date -Is)" "$1"; }

cutoff="$(date -I -d "$RETAIN_DAYS days ago")"
log "msg\":\"start\",\"data_root\":\"$DATA_ROOT\",\"retain_days\":$RETAIN_DAYS,\"cutoff\":\"$cutoff\",\"apply\":$APPLY"

manifest_ok() {  # $1=dataset $2=date
  [ "$1" = "-" ] && return 0
  [ -f "$MANIFEST" ] || return 1
  python3 - "$MANIFEST" "$1" "$2" <<'PY'
import json, sys
manifest, dataset, day = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    rows = json.load(open(manifest))
except Exception:
    sys.exit(1)
for e in rows:
    if isinstance(e, dict) and e.get("dataset") == dataset and e.get("date") == day \
            and (e.get("rows_kept") or 0) > 0:
        sys.exit(0)
sys.exit(1)
PY
}

freed=0
for entry in "${PRUNABLE[@]}"; do
  ds="${entry%%:*}"; need="${entry##*:}"
  root="$DATA_ROOT/raw/$ds"
  [ -d "$root" ] || continue
  for part in "$root"/dt=*; do
    [ -d "$part" ] || continue
    day="$(basename "$part")"; day="${day#dt=}"
    [[ "$day" < "$cutoff" ]] || continue
    if ! manifest_ok "$need" "$day"; then
      log "msg\":\"kept\",\"dataset\":\"$ds\",\"date\":\"$day\",\"reason\":\"no $need in manifest\""
      continue
    fi
    bytes="$(du -sb "$part" | cut -f1)"
    freed=$((freed + bytes))
    if [ "$APPLY" = "1" ]; then
      rm -rf "$part"
      log "msg\":\"pruned\",\"dataset\":\"$ds\",\"date\":\"$day\",\"bytes\":$bytes"
    else
      log "msg\":\"would_prune\",\"dataset\":\"$ds\",\"date\":\"$day\",\"bytes\":$bytes"
    fi
  done
done
log "msg\":\"done\",\"bytes_freed\":$freed,\"apply\":$APPLY"
