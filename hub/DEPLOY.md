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

## Networking (important — read before production)

emhub routes each incoming telemetry datagram to a line/EM by its **source
IP** (matched against each line's configured PLC host in `config.yaml`).
Docker's NAT rewrites the UDP source IP to the Docker gateway on published
ports, so every PLC would appear to come from one address and get dropped
("datagram for unconfigured EM").

**Linux production (recommended):** run emhub with host networking so it sees
real PLC IPs. Replace the `ports:` block on the `emhub` service with:

```yaml
    network_mode: host        # binds 8062/tcp + 15020/udp directly on the host
```

and change its DSN to reach the DB over the host loopback:

```yaml
    environment:
      EMHUB_DSN: "postgres://monitor:${DB_PASSWORD:-monitor}@127.0.0.1:5432/emhub"
```

leaving the `timescaledb` service on the default bridge with
`ports: ["127.0.0.1:5432:5432"]`. Host networking is Linux-only.

**Docker Desktop (Windows/Mac):** fine for development and demos, but inbound
UDP is always NAT'd and host networking attaches to the WSL2 VM, not your LAN —
so it cannot correctly receive plant telemetry from multiple PLCs by source IP.
For real PLC ingestion, deploy on a Linux host. (The legacy bare-`emhub.exe` on
Windows works only because it binds the host NIC directly, with no NAT layer.)

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
