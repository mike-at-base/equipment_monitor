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

## Networking

Each telemetry datagram self-identifies its `(line, station, em)` in the
payload (wire v4), so the collector does **not** depend on the source IP.
Docker's NAT on published ports is therefore fine — plain **bridge networking
works on Linux and Windows**, no host networking required. Just make sure:

- **UDP 15020** is open on the host firewall and the PLCs can route to it.
- **TCP 8062** is reachable for whoever views the UI.

```bash
sudo ufw allow 15020/udp
sudo ufw allow 8062/tcp
```

## Configuration

There is no hierarchy config file. EMs **auto-discover** from their telemetry
(each PLC's FB declares its `lineName`) and land in an *unconfirmed* state;
an engineer then reviews and confirms each one in the UI (name, line/station,
cycle metadata). The database is the single source of truth. The UDP listen
port is set via `EMHUB_UDP_PORT` (default 15020).

## Read-only database access (optional)

TimescaleDB publishes **TCP 5433→5432** on the host so reporting/ETL tools can
read the fact tables directly instead of polling the API. (Host **5432** is
often already taken by the v1 `equipment-monitor` stack; use **5433** for
emhub. Inside compose the DB remains `timescaledb:5432`.)

To hand a client a SELECT-only login, add to `hub/.env`:

```
EMHUB_READONLY_USER=reporting
EMHUB_READONLY_PASSWORD=<something-strong>
```

emhub creates the role on startup (re-syncing its password on later starts)
and grants it **SELECT only** — no INSERT/UPDATE/DELETE and no DDL — on every
table, including ones added by a future release. There is no default password:
leave both unset and the role is never created.

```bash
# only if a remote host reads the DB (not needed for a localhost poller)
sudo ufw allow 5433/tcp
psql "postgres://reporting:<password>@127.0.0.1:5433/emhub" -c '\dt'
```

For the BigQuery poller on this same host:

| Poller env | Value |
|---|---|
| `MFG_EMHUB_PG_HOST` | `127.0.0.1` |
| `MFG_EMHUB_PG_PORT` | `5433` |
| `MFG_EMHUB_PG_DATABASE` | `emhub` |
| `MFG_EMHUB_PG_USER` | `reporting` (or whatever you set) |
| `MFG_EMHUB_PG_PASSWORD` | the password from `.env` |
| `MFG_EMHUB_PG_SSLMODE` | `disable` (local Postgres has no TLS) |

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
