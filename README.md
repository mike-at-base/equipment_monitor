# Equipment Monitor

OPC UA data collector + Plotly Dash dashboard for Siemens S7-1500 equipment modules.

## Quick Start (Docker — recommended)

The full stack runs in three containers: TimescaleDB, the collector, and the Dash app.

```
docker compose up -d --build
```

That's the entire deploy.  Open **http://localhost:8050** in a browser.

The collector runs `init_schema()` on first boot, so the database is set up automatically.  `config.yaml` is bind-mounted into both the collector and the app, so edits on the host take effect after a container restart:

```
# Common operations
docker compose ps                       # see what's running
docker compose logs -f collector        # tail collector output
docker compose logs -f app              # tail app output
docker compose restart collector        # apply config.yaml changes
docker compose down                     # stop everything (data persists)
docker compose down -v                  # stop AND wipe the DB volume
```

### Dry-run inside the container
```
docker compose run --rm collector python -u collector/main.py --dry-run
```

---

## Running outside Docker (native Python)

If you'd rather run the collector / app directly (e.g. for hot-reload development):

1. **Start TimescaleDB only**
   ```
   docker compose up -d timescaledb
   ```
2. **Install Python dependencies**
   ```
   pip install -r requirements.txt
   ```
3. **Initialise the database schema** (collector does this too, but you can run it first)
   ```
   python db/schema.py
   ```
4. **(Optional) verify OPC UA node paths**
   ```
   python collector/main.py --dry-run
   ```
5. **Start the collector** (separate terminal)
   ```
   python collector/main.py
   ```
6. **Start the Dash app** (separate terminal)
   ```
   python app/main.py
   ```
   Open **http://localhost:8050** in a browser.

---

## Configuration

Edit `config.yaml` to add PLCs, stations, and equipment modules.
After saving, restart the collector (`Ctrl+C` then `python collector/main.py`).
You can also edit via the **⚙ Config** tab in the app.

### Adding a new PLC / cell
```yaml
plcs:
  - name: CELL2
    opc_endpoint: "opc.tcp://10.0.0.50:4840"
    enabled: true
    equipment_modules:
      - station: ST10000
        display_name: "My Station"
        em_db_path: "ST10000_Station_DB.mainEquipmentModule"
        em_label: main
        enabled: true
        sequences:
          - { index: 1, name: Home, is_production: false }
          - { index: 2, name: Run,  is_production: true  }
```

### `is_production: true`
Marks the sequence used for **cycle time** and **availability** tracking.
Each EM should have exactly one production sequence.

### `cycle_start_step` (optional)
Overrides which step marks cycle-start for start-to-start cycle time.
Defaults to `SEQUENCE_INITIAL_STEP` when omitted.

Example:
```yaml
sequences:
  - { index: 2, name: Run, is_production: true, cycle_start_step: "100-WORK-START" }
```

---

## OPC UA Sampling Rate

The collector requests **100 ms** publish and sampling intervals (push-based subscriptions).
The Siemens S7-1500 OPC UA server may cap this at its `MinimumSamplingInterval` (default 1000 ms).
To capture fast steps, ask the PLC engineer to lower it in TIA Portal:
> Project → PLC → OPC UA → Server → MinimumSamplingInterval → set to 10 ms

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `host=localhost port=5432 dbname=equipment user=monitor password=monitor` | PostgreSQL DSN.  Compose sets this to `host=timescaledb …` inside the container network. |
| `DASH_DEBUG` | `true` for native Python, `false` in Docker | Enables Dash's dev server (hot-reload + verbose errors).  Leave off in production — the dev server is not hardened against external traffic. |
