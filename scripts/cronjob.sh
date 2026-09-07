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
# `-E 99` separates "could not take the lock" from "the job itself failed",
# so a skip logs a structured job_skipped event and exits 0 (a skip is not a
# failure, and must not trip MAILTO), while a real failure keeps its own exit
# code and reaches Healthchecks as it always did.
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

flock -n -E 99 "$LOCK" "$@"
rc=$?

if [ "$rc" -eq 99 ]; then
  printf '{"ts":"%s","event":"job_skipped","job":"%s","reason":"previous run still holds %s"}\n' \
    "$(date -Is)" "$JOB" "$LOCK"
  exit 0
fi

exit "$rc"
