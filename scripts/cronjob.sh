#!/usr/bin/env bash
# Run one job under its non-blocking lock, and say so when a run is skipped.
#
# `flock -n` drops a run that would overlap the previous one, which is the
# behaviour we want -- but it drops it *silently*, exiting 1 with no output.
# On 2026-09-02 six scheduled trades_watchlist runs were discarded that way
# (14:10, 14:30, 14:50, 15:15, 15:25, 15:35 UTC) and nothing anywhere recorded
# it: cron.log showed a clean run every five minutes with gaps you had to
# diff against the schedule to notice. A job with no headroom left is exactly
# the thing that should be visible before it becomes a job that stops.
#
# The lock is taken on fd 9 *before* the command runs, so a command that
# exits 99 is no longer logged as job_skipped. `flock -n -E 99` is used
# only on that fd acquire: 99 is contention, other flock failures stay
# nonzero. Wrapping the command itself with `flock -n -E 99` used to
# swallow a real exit 99 to 0.
#
# BLAS threads are pinned because the SVI fit is not reproducible without it.
# Same code, same inputs, 1 thread against 8, on 2026-09-04: 397 of 720
# vol_surface values moved, svi_rho by up to 2.24e-03 relative, while
# rms_error moved less than 1e-09 -- the optimiser lands elsewhere in an
# equally good basin. A parameter set that depends on how busy the box was is
# one the backtester cannot reproduce and a rebuild cannot be diffed against.
# One thread rather than a fixed larger count: it is deterministic across
# machines too, and the fits are per-date parallel anyway. Element-wise numpy
# (the CRR trees in drift_check) does not go through BLAS, so this costs the
# other jobs nothing. `:=` leaves a deliberate override in place; the units
# never set it, so scheduled runs always get 1.
: "${OMP_NUM_THREADS:=1}"
: "${OPENBLAS_NUM_THREADS:=1}"
: "${MKL_NUM_THREADS:=1}"
: "${NUMEXPR_NUM_THREADS:=1}"
: "${VECLIB_MAXIMUM_THREADS:=1}"
export OMP_NUM_THREADS OPENBLAS_NUM_THREADS MKL_NUM_THREADS \
       NUMEXPR_NUM_THREADS VECLIB_MAXIMUM_THREADS

# Usage:  bash scripts/cronjob.sh <job-name> <command> [args...]
set -uo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: cronjob.sh <job-name> <command> [args...]" >&2
  exit 2
fi

JOB="$1"
shift
LOCK="/tmp/massive-${JOB}.lock"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Python jobs ping from ingest.common.cli.run_job. Bash jobs (prune) do not,
# so this wrapper pings for a `bash *.sh` command. `bash -c` is left alone:
# that is how tests (and ad-hoc one-liners) invoke the lock, not a scheduled
# job. A skip does not ping -- the holder of the lock owns the in-flight run.
_is_shell_job=0
if [ "${1:-}" = "bash" ]; then
  case "${2:-}" in
    *.sh) _is_shell_job=1 ;;
  esac
fi

_load_hc_env() {
  # Crontab does not source .env; systemd EnvironmentFile does. Fill only
  # variables that are unset. Empty-but-set must win so tests cannot leak a
  # real ping against production.
  local envf line key val
  envf="$REPO_ROOT/.env"
  [ -f "$envf" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      HEALTHCHECKS_PING_KEY=*|HEALTHCHECKS_BASE=*)
        key="${line%%=*}"
        if [ "$key" = "HEALTHCHECKS_PING_KEY" ] && [ -n "${HEALTHCHECKS_PING_KEY+x}" ]; then
          continue
        fi
        if [ "$key" = "HEALTHCHECKS_BASE" ] && [ -n "${HEALTHCHECKS_BASE+x}" ]; then
          continue
        fi
        val="${line#*=}"
        val="${val%$'\r'}"
        val="${val#\"}"
        val="${val%\"}"
        val="${val#\'}"
        val="${val%\'}"
        export "$key=$val"
        ;;
    esac
  done < "$envf"
}

_hc_ping() {
  # $1 suffix (/start, /fail, or empty)  $2 optional body
  # Never fails the job: monitoring must not be able to fail the thing it
  # monitors, matching ingest.common.cli.ping.
  local suffix="${1:-}"
  local body="${2:-}"
  local base slug url
  [ -z "${HEALTHCHECKS_PING_KEY:-}" ] && return 0
  if ! command -v curl >/dev/null 2>&1; then
    echo "warning: healthcheck ping skipped: curl not found" >&2
    return 0
  fi
  base="${HEALTHCHECKS_BASE:-https://hc-ping.com}"
  base="${base%/}"
  slug="massive-$(printf '%s' "$JOB" | tr 'A-Z' 'a-z' | tr '_' '-')"
  url="${base}/${HEALTHCHECKS_PING_KEY}/${slug}${suffix}?create=1"
  curl -sS -m 5 -o /dev/null --data "$body" "$url" 2>/dev/null || \
    echo "warning: healthcheck ping failed" >&2
  return 0
}

# Hold the lock on this shell's fd 9, then run the command in this same
# process. The command's exit status cannot be confused with "lock not taken".
if ! exec 9>"$LOCK"; then
  echo "error: cannot open lock $LOCK" >&2
  exit 1
fi
flock -n -E 99 9
flock_rc=$?
if [ "$flock_rc" -eq 99 ]; then
  printf '{"ts":"%s","event":"job_skipped","job":"%s","reason":"previous run still holds %s"}\n' \
    "$(date -Is)" "$JOB" "$LOCK"
  exit 0
fi
if [ "$flock_rc" -ne 0 ]; then
  echo "error: flock failed with status $flock_rc (lock $LOCK)" >&2
  exit "$flock_rc"
fi

if [ "$_is_shell_job" -eq 1 ]; then
  _load_hc_env
  _hc_ping "/start"
fi

"$@"
rc=$?

if [ "$_is_shell_job" -eq 1 ]; then
  if [ "$rc" -eq 0 ]; then
    _hc_ping "" "$JOB ok"
  else
    _hc_ping "/fail" "$JOB exited $rc"
  fi
fi

exit "$rc"
