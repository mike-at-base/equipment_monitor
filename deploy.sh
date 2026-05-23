#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — install Equipment Monitor on a Linux server
#
# Usage (run from the cloned repo):
#   chmod +x deploy.sh
#   ./deploy.sh
#
# Prerequisites:
#   - Python 3.11+  (python3)
#   - Docker + Docker Compose plugin
#   - sudo access (for systemd service installation)
#
# On re-deploy (updates): pull the repo and re-run. The script is idempotent.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
SERVICE_USER="${SERVICE_USER:-$(whoami)}"

echo "=== Equipment Monitor deploy ==="
echo "    Install dir : $INSTALL_DIR"
echo "    Python      : $($PYTHON --version)"
echo "    Service user: $SERVICE_USER"
echo ""

# ── 1. Python dependencies ────────────────────────────────────────────────────
echo "[1/4] Installing Python dependencies..."
"$PYTHON" -m pip install --quiet -r "$INSTALL_DIR/requirements.txt"

# ── 2. TimescaleDB ────────────────────────────────────────────────────────────
echo "[2/4] Starting TimescaleDB container..."
cd "$INSTALL_DIR"
docker compose up -d

echo "      Waiting for database to accept connections..."
until docker exec equipment-monitor-timescaledb-1 \
    pg_isready -U monitor -d equipment &>/dev/null; do
    sleep 1
    printf '.'
done
echo " ready."

# ── 3. Database schema ────────────────────────────────────────────────────────
echo "[3/4] Initialising database schema..."
cd "$INSTALL_DIR"
"$PYTHON" db/schema.py

# ── 4. Systemd services ───────────────────────────────────────────────────────
echo "[4/4] Installing systemd services..."
PYTHON_BIN="$(which "$PYTHON")"
UNIT_DIR=/etc/systemd/system

for svc in equipment-collector equipment-app; do
    sed \
        -e "s|{{INSTALL_DIR}}|$INSTALL_DIR|g" \
        -e "s|{{PYTHON}}|$PYTHON_BIN|g"       \
        -e "s|{{USER}}|$SERVICE_USER|g"        \
        "$INSTALL_DIR/systemd/$svc.service"    \
    | sudo tee "$UNIT_DIR/$svc.service" > /dev/null
    echo "      Wrote $UNIT_DIR/$svc.service"
done

sudo systemctl daemon-reload
sudo systemctl enable equipment-collector equipment-app
sudo systemctl restart equipment-collector equipment-app

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Deploy complete ==="
echo ""
echo "    Dashboard : http://$(hostname -I | awk '{print $1}'):8050"
echo ""
echo "    Status    : sudo systemctl status equipment-collector equipment-app"
echo "    App logs  : journalctl -u equipment-app -f"
echo "    Collector : journalctl -u equipment-collector -f"
echo ""
echo "    To update : git pull && ./deploy.sh"
