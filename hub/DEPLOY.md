# Deploying emhub on a Linux server

emhub (the v2 SCADA + telemetry ingest) ships as one static Go binary with the
web UI compiled in. The only dependency is TimescaleDB, which the compose file
runs for you. Everything builds from a clean checkout — no Go, Node, or build
tools needed on the server, just Docker.

## Quick start

```bash
git clone <repo-url> equipment-monitor
cd equipment-monitor/hub
docker compose up -d --build
```

That's it. Open `http://<server>:8062` for the SCADA UI.

- **UI + API:** `http://<server>:8062`
- **PLC telemetry:** point each `EquipmentModuleTelemetry` FB's `remoteIp*` at
  the server and `remotePort` at **15020/udp**.

## Before production

1. **Set a real DB password.** Create `hub/.env`:
   ```
   DB_PASSWORD=<something-strong>
   APP_TIMEZONE=America/Chicago
   ```
   Both the database and emhub read it — no need to edit the compose file.

2. **Open the firewall** for inbound **UDP 15020** from the plant network and
   **TCP 8062** for whoever views the UI:
   ```bash
   sudo ufw allow 15020/udp
   sudo ufw allow 8062/tcp
   ```

3. **Confirm the PLCs can route to the server** on UDP 15020 (this is the one
   thing Docker can't do for you — it's plant-network reachability).

## Configuration

emhub reads `../config.yaml` (the repo-root config) for lines, EMs, and
`telemetry.listen_port`. It's bind-mounted read-only, so edit it on the host
and re-sync with:

```bash
docker compose restart emhub
```

emhub upserts the hierarchy and auto-creates its schema on startup — no manual
migrations.

## Operations

```bash
docker compose logs -f emhub          # follow logs
docker compose ps                     # status
docker compose pull && \
  docker compose up -d --build        # update after a git pull
docker compose down                   # stop (data survives in the volume)
```

Telemetry history lives in the `emhub_data` named volume. Back it up with:

```bash
docker run --rm -v emhub_emhub_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/emhub-db-$(date +%F).tgz -C /data .
```

## Notes

- The build is fully reproducible: the UI is rebuilt inside the image every
  time, so the committed `web/dist/` is never trusted for a deploy.
- The binary runs as a non-root user on distroless; ports 8062 and 15020 are
  both > 1024, so no extra privileges are needed.
- This stack is independent of the legacy v1 Python collector/Dash compose at
  the repo root; run one or the other.
