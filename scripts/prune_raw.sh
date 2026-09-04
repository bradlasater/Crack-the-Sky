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
# QUARANTINE
#   _quarantine/** holds output that a re-filter or re-pull has already
#   superseded on disk -- by construction, the copy that replaced it is in
#   clean/. It had no retention path at all and had grown to 22 GB, which is
#   most of a re-filter's worth of dead weight. It is pruned here on its own
#   clock (QUARANTINE_RETAIN_DAYS, default 30) because its ages are the dates
#   of the *runs* that quarantined it, not of the market data inside.
#
# FLAT FILES (opt-in, --flatfiles)
#   raw/flatfiles is 65 GB and is deliberately NOT pruned by default: it is
#   the authoritative vendor payload, and the repo treats it as the record
#   everything else reconciles against. It is however re-downloadable from S3
#   -- that is the whole reason trades and bars count as backfillable -- so
#   --flatfiles enables pruning it under a deliberately long retention
#   (FLATFILE_RETAIN_DAYS, default 365) and only for dates the manifest shows
#   were parsed successfully. Turn it on when disk pressure warrants it;
#   leaving it off keeps the current behaviour exactly.
#
# A partition is only pruned when the flat file that replaces it is recorded
# in _meta/flatfile_manifest.json with rows kept, so a failed pull can never
# lead to deleting the only copy.
#
# Usage:  bash scripts/prune_raw.sh [--apply] [--flatfiles]
#           (dry-run without --apply)
#         RETAIN_DAYS=90 QUARANTINE_RETAIN_DAYS=30 bash scripts/prune_raw.sh --apply
set -euo pipefail

DATA_ROOT="${DATA_ROOT:-/data/massive}"
RETAIN_DAYS="${RETAIN_DAYS:-90}"
QUARANTINE_RETAIN_DAYS="${QUARANTINE_RETAIN_DAYS:-30}"
FLATFILE_RETAIN_DAYS="${FLATFILE_RETAIN_DAYS:-365}"
MANIFEST="$DATA_ROOT/_meta/flatfile_manifest.json"
APPLY=0
PRUNE_FLATFILES=0
for arg in "$@"; do
  case "$arg" in
    --apply)      APPLY=1 ;;
    --flatfiles)  PRUNE_FLATFILES=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# These knobs feed deletion cutoffs. A non-numeric value crashes date(1)
# mid-run; a negative one moves the cutoff into the future and condemns
# everything. Reject anything that is not a non-negative integer up front.
for knob in RETAIN_DAYS QUARANTINE_RETAIN_DAYS FLATFILE_RETAIN_DAYS; do
  case "${!knob}" in
    ''|*[!0-9]*)
      echo "prune_raw: $knob must be a non-negative integer (got '${!knob}')" >&2
      exit 2
      ;;
  esac
done

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

# The manifest is read ONCE into a lookup file. It used to be re-parsed by a
# fresh python process for every candidate partition, which is fine for the
# ~90 days of the prunable sets but takes minutes across the ~3,000
# dataset-days under raw/flatfiles.
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

manifest_ok() {  # $1=dataset $2=date
  [ "$1" = "-" ] && return 0
  grep -qxF "$1|$2" "$MANIFEST_INDEX"
}

freed=0
for entry in "${PRUNABLE[@]}"; do
  ds="${entry%%:*}"; need="${entry##*:}"
  root="$DATA_ROOT/raw/$ds"
  [ -d "$root" ] || continue
  for part in "$root"/dt=*; do
    [ -d "$part" ] || continue
    day="$(basename "$part")"; day="${day#dt=}"
    # Only real calendar dates. The shape check alone passes 2023-99-99,
    # which sorts before any cutoff; underlying_day_bars has no manifest
    # gate, so round-trip the date through date(1) before trusting the
    # string comparison below.
    case "$day" in
      [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
      *) continue ;;
    esac
    [ "$(date -I -d "$day" 2>/dev/null)" = "$day" ] || continue
    [[ "$day" < "$cutoff" ]] || continue
    if ! manifest_ok "$need" "$day"; then
      log "msg\":\"kept\",\"dataset\":\"$ds\",\"date\":\"$day\",\"reason\":\"no $need in manifest\""
      continue
    fi
    bytes="$(du -sb "$part" | cut -f1)"
    freed=$((freed + bytes))
    if [ "$APPLY" = "1" ]; then
      rm -rf "$part"
      log "msg\":\"pruned\",\"dataset\":\"$ds\",\"date\":\"$day\",\"path\":\"$part\",\"bytes\":$bytes"
    else
      log "msg\":\"would_prune\",\"dataset\":\"$ds\",\"date\":\"$day\",\"path\":\"$part\",\"bytes\":$bytes"
    fi
  done
done
# --- quarantine: superseded output, replaced on disk by construction -------
qcutoff="$(date -I -d "$QUARANTINE_RETAIN_DAYS days ago")"
qroot="$DATA_ROOT/_quarantine"
if [ -d "$qroot" ]; then
  # Age the individual dt= batches, never the bucket above them. The two
  # layouts in use nest the date at different depths --
  #   _quarantine/pre-root-filter/dt=<date>/<dataset>/
  #   _quarantine/refilter/<dataset>/dt=<date>/
  # -- so the batch is found by name at any depth rather than by position.
  #
  # Pruning the top-level bucket instead would be destructive: a directory's
  # mtime does not change when files are added *below* an existing child, so
  # refilter/ can look untouched for months while fresh output lands under
  # refilter/<dataset>/. Deleting it wholesale would take the last 30 days of
  # quarantined output with it -- exactly the data someone would reach for.
  #
  # mtime, not the dt= value: dt= is the market date of the data, while
  # retention here is about how long ago it was superseded.
  while IFS= read -r batch; do
    [ -d "$batch" ] || continue
    mtime="$(date -I -r "$batch")"
    [[ "$mtime" < "$qcutoff" ]] || continue
    bytes="$(du -sb "$batch" | cut -f1)"
    freed=$((freed + bytes))
    if [ "$APPLY" = "1" ]; then
      rm -rf "$batch"
      log "msg\":\"pruned\",\"dataset\":\"_quarantine\",\"path\":\"$batch\",\"bytes\":$bytes"
    else
      log "msg\":\"would_prune\",\"dataset\":\"_quarantine\",\"path\":\"$batch\",\"bytes\":$bytes"
    fi
  done < <(find "$qroot" -mindepth 2 -type d -name 'dt=*')

  # Buckets and dataset directories left empty by the above carry nothing;
  # -depth so children are considered before their parents. Only ever removes
  # already-empty directories, and never $qroot itself.
  if [ "$APPLY" = "1" ]; then
    find "$qroot" -mindepth 1 -depth -type d -empty -delete 2>/dev/null || true
  fi
fi

# --- flat files: opt-in, re-downloadable from S3 ---------------------------
if [ "$PRUNE_FLATFILES" = "1" ]; then
  fcutoff="$(date -I -d "$FLATFILE_RETAIN_DAYS days ago")"
  froot="$DATA_ROOT/raw/flatfiles"
  if [ -d "$froot" ]; then
    # Layout is raw/flatfiles/<dataset>/dt=<date>/<date>.csv.gz, so each
    # dataset-day is pruned against its own manifest entry rather than
    # requiring all three to have landed.
    while IFS= read -r part; do
      day="$(basename "$part")"; day="${day#dt=}"
      ds="$(basename "$(dirname "$part")")"
      case "$day" in
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
        *) continue ;;
      esac
      [[ "$day" < "$fcutoff" ]] || continue
      if ! manifest_ok "$ds" "$day"; then
        log "msg\":\"kept\",\"dataset\":\"flatfiles/$ds\",\"date\":\"$day\",\"reason\":\"no $ds in manifest\""
        continue
      fi
      bytes="$(du -sb "$part" | cut -f1)"
      freed=$((freed + bytes))
      if [ "$APPLY" = "1" ]; then
        rm -rf "$part"
        log "msg\":\"pruned\",\"dataset\":\"flatfiles/$ds\",\"date\":\"$day\",\"path\":\"$part\",\"bytes\":$bytes"
      else
        log "msg\":\"would_prune\",\"dataset\":\"flatfiles/$ds\",\"date\":\"$day\",\"path\":\"$part\",\"bytes\":$bytes"
      fi
    done < <(find "$froot" -mindepth 2 -maxdepth 2 -type d -name 'dt=*')
  fi
fi

log "msg\":\"done\",\"bytes_freed\":$freed,\"apply\":$APPLY"
