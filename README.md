# Equipment Monitor

OPC UA data collector + Plotly Dash dashboard for Siemens S7-1500 equipment modules.

## Quick Start

### 1. Start TimescaleDB
```
docker compose up -d
```

### 2. Install Python dependencies
```
pip install -r requirements.txt
```

### 3. Initialise the database schema
```
python db/schema.py
```

### 4. Verify OPC UA node paths (optional dry run)
```
python collector/main.py --dry-run
```
This browses the PLC namespace and prints every resolved node without writing any data.

### 5. Start the collector (separate terminal)
```
python collector/main.py
```

### 6. Start the Dash app (separate terminal)
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
| `DATABASE_URL` | `host=localhost port=5432 dbname=equipment user=monitor password=monitor` | PostgreSQL DSN |
