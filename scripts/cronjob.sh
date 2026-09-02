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
