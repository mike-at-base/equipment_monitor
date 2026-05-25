#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — install / update Equipment Monitor on a Linux server.
# The full stack (TimescaleDB + collector + Dash app) runs in containers
# via docker compose.  Idempotent — re-run after a `git pull` to update.
#
# Usage (run from the cloned repo):
#   chmod +x deploy.sh
#   ./deploy.sh
#
# Prerequisites:
#   - Docker Engine + Docker Compose plugin (https://docs.docker.com/engine/install/)
#   - The user running this script in the `docker` group (or use sudo)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "=== Equipment Monitor deploy ==="
echo "    Install dir : $INSTALL_DIR"
echo ""

cd "$INSTALL_DIR"

# ── Pre-flight checks ─────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || {
    echo "ERROR: docker not found.  Install Docker Engine first:"
    echo "       https://docs.docker.com/engine/install/"
    exit 1
}
docker compose version >/dev/null 2>&1 || {
    echo "ERROR: docker compose plugin not found.  Install it via:"
    echo "       apt install docker-compose-plugin    (Debian/Ubuntu)"
    exit 1
}

# ── 1. Migrate from a previous native-Python deploy, if present ──────────────
# Older versions of this repo installed equipment-collector.service and
# equipment-app.service as systemd units that ran `python collector/main.py`
# and `python app/main.py` directly.  Those would race the new Docker stack
# for port 8050 and double-write into the DB, so stop and disable them
# before bringing the containers up.
if systemctl list-unit-files 2>/dev/null | grep -q '^equipment-'; then
    echo "[1/3] Detected pre-existing systemd units from a native deploy."
    echo "      Stopping and disabling so the Docker stack can take over..."
    sudo systemctl stop    equipment-collector equipment-app 2>/dev/null || true
    sudo systemctl disable equipment-collector equipment-app 2>/dev/null || true
    sudo rm -f /etc/systemd/system/equipment-collector.service \
               /etc/systemd/system/equipment-app.service
    sudo systemctl daemon-reload
    echo "      Done."
else
    echo "[1/3] No pre-existing native services to clean up."
fi

# ── 2. Build images + bring up the full stack ────────────────────────────────
# `up -d --build` rebuilds the image whenever the Dockerfile or any source
# under the build context has changed.  The compose healthcheck on
# timescaledb gates the collector and app, and collector/main.py runs
# init_schema() on first boot so DB tables are created automatically.
echo "[2/3] Building images and bringing up containers..."
docker compose up -d --build

# ── 3. Wait for the dashboard to respond ─────────────────────────────────────
echo "[3/3] Waiting for the dashboard to come up..."
for i in $(seq 1 60); do
    if curl -fsS -o /dev/null http://localhost:8050; then
        echo "      Ready."
        break
    fi
    sleep 2
    printf '.'
done
echo ""

# ── Summary ──────────────────────────────────────────────────────────────────
IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost)"

echo ""
echo "=== Deploy complete ==="
echo ""
echo "    Dashboard  : http://${IP}:8050"
echo ""
echo "    Status     : docker compose ps"
echo "    App logs   : docker compose logs -f app"
echo "    Collector  : docker compose logs -f collector"
echo "    DB logs    : docker compose logs -f timescaledb"
echo "    Restart    : docker compose restart"
echo "    Update     : git pull && ./deploy.sh"
echo "    Stop all   : docker compose down        # data persists"
echo "    Wipe DB    : docker compose down -v     # loses all data"
echo ""
