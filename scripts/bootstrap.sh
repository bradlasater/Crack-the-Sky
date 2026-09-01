#!/usr/bin/env bash
# bootstrap.sh — idempotent setup for the headless Ubuntu 24.04 box.
# Safe to re-run: existing venv/.env are left untouched.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

echo "==> [1/5] Checking python3 >= 3.11"
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found. Install it: sudo apt install python3 python3-venv" >&2
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "ERROR: python3 >= 3.11 required; found: $(python3 --version 2>&1)" >&2
    echo "       On Ubuntu: sudo apt install python3.12 python3.12-venv" >&2
    exit 1
fi
python3 --version

echo "==> [2/5] Creating venv (if missing) and installing dependencies"
if [ ! -x "$REPO/venv/bin/python" ]; then
    python3 -m venv "$REPO/venv"
fi
"$REPO/venv/bin/pip" install --upgrade pip >/dev/null
"$REPO/venv/bin/pip" install -r "$REPO/requirements.txt"

echo "==> [3/5] Checking .env"
if [ ! -f "$REPO/.env" ]; then
    cp "$REPO/.env.example" "$REPO/.env"
    echo "WARNING: .env created from .env.example — EDIT IT NOW:"
    echo "         - set MASSIVE_API_KEY"
    echo "         - set MASSIVE_S3_ACCESS_KEY_ID / MASSIVE_S3_SECRET_ACCESS_KEY"
    echo "           (Massive dashboard -> S3 Access Keys; placeholders will fail)"
else
    echo "     .env already present (left untouched)"
fi

echo "==> [4/5] Checking clock sync (trading timestamps need accurate NTP)"
if command -v timedatectl >/dev/null 2>&1; then
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qi yes; then
        echo "     NTP synchronized: yes"
    else
        echo "WARNING: NTP not synchronized. Fix with:" >&2
        echo "         sudo apt install chrony && sudo systemctl enable --now chronyd" >&2
        echo "         (or: sudo timedatectl set-ntp true)" >&2
    fi
else
    echo "NOTE: timedatectl unavailable; ensure the clock is NTP-synced (chronyd)."
fi

echo "==> [5/5] Data/log roots"
DATA_ROOT="$(grep -E '^DATA_ROOT=' "$REPO/.env" | cut -d= -f2 || true)"
DATA_ROOT="${DATA_ROOT:-/data/massive}"
if [ ! -d "$DATA_ROOT" ]; then
    echo "NOTE: DATA_ROOT=$DATA_ROOT does not exist yet. Create it, e.g.:"
    echo "      sudo mkdir -p $DATA_ROOT && sudo chown \$USER $DATA_ROOT"
    echo "      (jobs also create it on first write when permissions allow)"
fi

cat <<EOF

Bootstrap complete. Next steps:
  1. Edit .env: real MASSIVE_API_KEY and the S3 keys from the Massive dashboard.
  2. Smoke-test live connectivity:   $REPO/venv/bin/python -m ingest.entitlements
  3. Install the schedule:
       sed -i "s|/home/brad-lasater|\$HOME|g" deploy/crontab
       crontab deploy/crontab
  4. (Optional) backfill history:    bash scripts/backfill.sh 2026-08-01 2026-08-31
EOF
