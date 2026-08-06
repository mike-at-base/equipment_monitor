# emhub API — for agents, integrations, and data export

There is no “Download CSV” button in the UI. **Everything the screens show
is available as JSON over HTTP** — same numbers, same windows. Point a
browser, `curl`, Python, or Excel Power Query at the hub and pull what you
need.

Base URL (local Docker default): `http://localhost:8062`  
On the plant host, replace `localhost` with that machine’s name/IP.

## The design rule

**The math lives in the API.** Availability, cycle statistics, flow losses,
MTTR, and composed availability are all computed server-side. The web UI,
the MCP tools, and `emctl` are thin readers of the same endpoints — so a
bot and a human looking at the same window can never disagree on a number.

Corollary for integrators: **prefer the aggregated fields** (`state_min`,
`flow_reasons`, cycle `stats`, …) when you want the plant definition of a
metric. Use the raw arrays (`intervals`, `steps`, `cycles`, `episodes`) when
you need to slice the history yourself in a notebook.

Two ways in:

| | Use it for |
|---|---|
| **REST** (`/api/v2/...`) | Scripts, notebooks, Power BI, anything else. **Start here for analysis.** |
| **MCP** (`POST /mcp`) | LLM agents. Tool-shaped, self-describing, read-only. |

---

## Pulling data for analysis

### Quick start

1. Find the exact line / station / EM spelling:
   ```bash
   curl -s http://localhost:8062/api/v2/hierarchy
   ```
2. Pick a time window (`today`, `8h`, `24h`, `3d`, `prod`, or `from`/`to`).
3. Hit the endpoint that matches the question (table below).
4. Save the JSON, or pipe through Python/`jq` into CSV.

**Windows note:** use `curl.exe` in PowerShell (plain `curl` is an alias for
`Invoke-WebRequest`). Or use the Python snippets further down.

### Which endpoint for which question?

| I want… | Endpoint | Notes |
|---|---|---|
| Line rollup (availability, top reasons, flow losses, per-EM) | `GET /api/v2/lines/{line}/summary?window=` | Best first stop for a shift report. |
| One EM’s availability + down + flow reasons | `GET /api/v2/ems/{line}/{station}/{label}/downs?window=` | Includes `state_min`, `flow_reasons`, `flow_reasons_timeline`, episodes. |
| Raw state timeline (every productive/starved/blocked/down…) | `GET .../intervals?window=` | Up to **2000** rows (newest first). Optional `?state=starved`. |
| Step history | `GET .../steps?window=&limit=&offset=` | Paginated; see below. |
| Cycle times | `GET .../cycles?window=` | `{ stats, cycles[] }`. |
| Cycles per hour / 15m | `GET .../throughput?window=&bucket=1h` | `bucket`: `15m`, `30m`, `1h`. |
| Composed (k-of-n) station availability + causes | `GET /api/v2/lines/{line}/stations/{station}/composed?window=` | Time-domain redundancy, not %×%. |
| What’s live right now | `GET /api/v2/live` | Snapshot of every EM. |

`{label}` is the EM label (`main`, `rb01`, …). Default in MCP is `main`;
in REST you must include it in the path.

### Time windows (every historical endpoint)

| Value | Meaning |
|---|---|
| *(omitted)* or `today` | Local midnight → now |
| `8h`, `30m`, `3d` | Rolling duration back from now |
| `prod` | Today’s **scheduled production** span for that line |
| `?from=&to=` | Explicit RFC3339 range (`to` defaults to now) |

Local midnight uses `APP_TIMEZONE` (default `America/Chicago`).
Timestamps in responses are **RFC3339 UTC**. Durations: `*_ms` = milliseconds,
`*_min` / `minutes` = minutes.

Example with an explicit range:

```bash
curl.exe -s "http://localhost:8062/api/v2/ems/MOD1/ST22000/main/downs?from=2026-08-04T12:00:00Z&to=2026-08-04T20:00:00Z"
```

### EM downs — availability, flow reasons, episodes

`GET /api/v2/ems/{line}/{station}/{label}/downs?window=`

Useful fields:

| Field | What it is |
|---|---|
| `from`, `to` | Exact window the numbers use |
| `availability_pct` | Episode-based availability (`null` if no production/data — not zero) |
| `production_min` | Minutes of scheduled production in the window |
| `state_min` | Minutes in each state (`productive`, `starved`, `blocked`, `down`, …) |
| `flow_reasons[]` | Starved/blocked **pareto** by waiting-on reason: `{ reason, state, minutes, count }` |
| `flow_reasons_timeline` | Same reasons **over time** (see below) |
| `top_reasons[]` | Down-episode reason pareto |
| `episodes[]` | Sticky root-cause down episodes (ack, retries, response/repair minutes) |
| `raw_downs[]` | Raw down intervals (every fault blip, not collapsed) |

Optional: `?flow_bucket=15m|30m|1h|4h|1d` overrides the timeline bucket.
If omitted, the hub picks one from the window span (≤4h → 15m, ≤36h → 1h,
≤7d → 4h, else 1d).

`flow_reasons_timeline` shape:

```json
{
  "bucket": "1h",
  "buckets": ["2026-08-04T12:00:00Z", "2026-08-04T13:00:00Z"],
  "series": [
    {
      "reason": "AGV Present (Sensor); AGV Present (Carrier)",
      "state": "starved",
      "minutes": [55.2, 60.0]
    }
  ]
}
```

`series[].minutes[i]` aligns with `buckets[i]`. Top 6 reasons are kept;
the rest collapse into `"Other"`.

### Steps — paginated raw history

`GET /api/v2/ems/{line}/{station}/{label}/steps?window=&limit=&offset=`

```json
{
  "steps": [
    {
      "start_ts": "...", "end_ts": "...", "seq_index": 1,
      "step": "100", "description": "Wait for Part",
      "duration_ms": 1234, "was_faulted": false
    }
  ],
  "total": 922,
  "limit": 1000,
  "offset": 0,
  "next_offset": 1000
}
```

- Newest first.
- REST default `limit` = 1000, max **20000**.
- When `next_offset` is present, request again with `offset=next_offset`
  until it disappears — that is the full history for the window.

### Intervals — raw state timeline

`GET /api/v2/ems/{line}/{station}/{label}/intervals?window=`  
Optional: `&state=starved` (or `blocked`, `down`, …).

Each row: `start_ts`, `end_ts`, `state`, `reason_type`, `reason`, `step_name`,
`ack_ts`. Capped at **2000** rows (newest first), plus the live open
interval when it matches. For multi-day exports of a busy EM, prefer
`downs` aggregates or page with tighter `from`/`to` windows.

### Cycles

`GET /api/v2/ems/{line}/{station}/{label}/cycles?window=`

```json
{
  "stats": { "count": 120, "p50_ms": 45000, "p90_ms": 61000, "interrupted": 3, "...": "..." },
  "cycles": [
    { "start_ts": "...", "end_ts": "...", "seq_index": 1,
      "work_ms": 30000, "exchange_ms": 15000, "total_ms": 45000, "interrupted": false }
  ]
}
```

### Export recipes

**Save one EM’s downs JSON (PowerShell):**
```powershell
curl.exe -s "http://localhost:8062/api/v2/ems/MOD1/ST22000/main/downs?window=8h" `
  -o downs_ST22000.json
```

**All steps for a window → CSV (Python):**
```python
import csv, json, urllib.request

BASE = "http://localhost:8062"
LINE, STATION, EM = "MOD1", "ST22000", "main"
WINDOW = "8h"

def get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return json.load(r)

rows, offset, limit = [], 0, 5000
while True:
    page = get(
        f"/api/v2/ems/{LINE}/{STATION}/{EM}/steps"
        f"?window={WINDOW}&limit={limit}&offset={offset}"
    )
    rows.extend(page["steps"])
    nxt = page.get("next_offset")
    if nxt is None:
        break
    offset = nxt

with open("steps.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys() if rows else [])
    if rows:
        w.writeheader()
        w.writerows(rows)
print(f"wrote {len(rows)} of {page['total']} steps")
```

**Flow reasons pareto → CSV (Python):**
```python
import csv, json, urllib.request

url = ("http://localhost:8062/api/v2/ems/MOD1/ST22000/main/downs"
       "?window=8h")
data = json.load(urllib.request.urlopen(url))

with open("flow_reasons.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["state", "reason", "minutes", "count"])
    w.writeheader()
    w.writerows(data.get("flow_reasons") or [])
```

**Flow reasons over time → long CSV (Python):**
```python
import csv, json, urllib.request

data = json.load(urllib.request.urlopen(
    "http://localhost:8062/api/v2/ems/MOD1/ST22000/main/downs?window=8h"))
tl = data["flow_reasons_timeline"]

with open("flow_reasons_timeline.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["bucket_ts", "bucket", "state", "reason", "minutes"])
    for s in tl["series"]:
        for ts, mins in zip(tl["buckets"], s["minutes"]):
            if mins:
                w.writerow([ts, tl["bucket"], s["state"], s["reason"], mins])
```

**Line summary slice (`jq`):**
```bash
curl.exe -s "http://localhost:8062/api/v2/lines/MOD1/summary?window=today" \
  | jq "{availability_pct, top_down_reasons, flow_losses, state_min}"
```

---

## MCP (agents)

Connect Claude Code:

```bash
claude mcp add --transport http emhub http://<host>:8062/mcp
```

Streamable HTTP, single JSON responses — no SSE. Protocol `2025-06-18`.
Every tool is an in-process wrapper over a REST endpoint (no network hop),
and **every tool is read-only** — there is no MCP path that can change
configuration or delete anything.

For live tiles, use the SSE stream (`GET /api/v2/stream`) rather than
polling `live_status` in a loop.

### Tools

Seventeen tools covering every read endpoint of the query API.

**Discovery**

| Tool | Arguments | Returns |
|---|---|---|
| `list_lines` | — | Lines with EM counts + live state rollup. **Start here.** |
| `hierarchy` | — | Full line → station → EM tree. Use to resolve exact spellings. |
| `live_status` | — | Every EM right now: state, reason, current step, dwell. |
| `unconfirmed_ems` | — | Auto-discovered EMs nobody has reviewed yet. |

**Line and station**

| Tool | Arguments | Returns |
|---|---|---|
| `line_summary` | `line`, `window` | **Composed** availability % + top down reasons (wall-clock line downtime; concurrent identical EM reasons count once), plus `em_avg_availability_pct`, minutes per state, cycle stats, flow losses, MTTR, per-EM breakdown. |
| `compare_lines` | `a`, `b`, `window` | Both summaries plus a delta. The tool for *"why is A running better than B"*. |
| `line_composed_availability` | `line`, `window` | Composed k-of-n availability for the line + each station. |
| `station_composed_availability` | `line`, `station`, `window` | Station %, up spans, down segments naming the EMs that broke the requirement, cause pareto, model in use. |
| `availability_model` | `line`, `station?` | The configured redundancy model, the default, and the member names. Omit `station` for the line-level model. |
| `line_schedule` | `line` | Weekly production shifts (drives E10 availability and the `prod` window). |

**Equipment module**

| Tool | Arguments | Returns |
|---|---|---|
| `em_downs` | `line`, `station`, `em_label`, `window` | Availability, `state_min`, flow-reason pareto + timeline, down episodes, raw downs. |
| `em_cycles` | ″ | Cycle records + stats, interrupted count. |
| `em_steps` | ″ + `limit?`, `offset?` | Paginated step history (`{steps, total, limit, offset, next_offset?}`). Default limit 500 (max 5000); page with `offset=next_offset` until `next_offset` is absent. |
| `em_intervals` | ″ | Raw state timeline with reasons. |
| `em_throughput` | ″ + `bucket` | Completed cycle counts per bucket (`15m`/`30m`/`1h`). |
| `em_debug` | ″ | Latest raw datagram (decoded bits, clock skew), resets, mode windows. |
| `em_config` | `line`, `station`, `em_label` | Sequence config + the step names actually observed. |

`em_label` defaults to `main`. `window` defaults to `today`.

### Calling it directly

```bash
curl -s -X POST http://localhost:8062/mcp -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
        "name":"em_downs",
        "arguments":{"line":"CELL1","station":"ST34000","em_label":"MAG01","window":"8h"}}}'
```

The payload comes back as JSON **inside** `result.content[0].text` (MCP
wraps tool output as text), with `result.isError` set when the underlying
REST call returned ≥ 400. Parse the inner string:

```python
body = json.loads(resp["result"]["content"][0]["text"])
```

### Guidance for agent prompts

- Discover before you query: `list_lines` → `line_summary` → drill into a
  specific EM. Line and station names are case-insensitive in paths.
- When explaining how a line is running, use `line_summary`'s
  `availability_pct` and `top_down_reasons` — both are **composed**
  (k-of-n wall-clock). Do not sum per-EM down minutes. `top_down_reasons`
  and `flow_losses` carry the actual failing permissive conditions from the
  PLC scan — that is the answer to "why", not the percentage alone.
- `starved` and `blocked` are *not* equipment failures — they mean the
  equipment was fine and starved of parts or blocked downstream. Blaming a
  machine for its starved time is the most common misreading.

---

## REST

Base: `http://<host>:8062`. All responses `application/json`; errors are
`{"error": "..."}` with a 4xx/5xx status.

### Read

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness. Returns `ok`. |
| `GET /api/v2/lines` | Lines + live state rollup. |
| `GET /api/v2/hierarchy` | Full line → station → EM tree with display names and confirmed flags. |
| `GET /api/v2/live` | Snapshot of every EM's current state. |
| `GET /api/v2/stream` | **SSE**, 1 Hz — same payload as `/live`, pushed. Use this instead of polling. |
| `GET /api/v2/unconfirmed` | Auto-discovered EMs awaiting review. |
| `GET /api/v2/lines/{line}/summary` | Full line summary (the big one). |
| `GET /api/v2/compare?a={line}&b={line}` | Decomposed A/B delta. |
| `GET /api/v2/lines/{line}/composed` | Composed (k-of-n) availability: line % + per-station %. |
| `GET /api/v2/lines/{line}/stations/{station}/composed` | Station composed %, up spans, down segments with causes, cause pareto. |
| `GET /api/v2/lines/{line}/schedule` | Weekly production shifts. |
| `GET /api/v2/lines/{line}/availmodel` | Line redundancy model (+ the default, + member names). |
| `GET /api/v2/lines/{line}/stations/{station}/availmodel` | Station redundancy model. |
| `GET /api/v2/ems/{line}/{station}/{label}/intervals` | Raw state timeline (≤2000 rows). Optional `?state=`. |
| `GET .../steps?limit=&offset=` | Paginated step history: `{steps, total, limit, offset, next_offset?}`. Newest first; limit default 1000 (max 20000). |
| `GET .../cycles` | Cycles + stats. |
| `GET .../throughput?bucket=15m\|30m\|1h` | Counts per bucket. |
| `GET .../downs` | Availability, `state_min`, `flow_reasons`, `flow_reasons_timeline`, down episodes, raw downs. Optional `?flow_bucket=`. |
| `GET .../debug` | Raw telemetry, resets, mode windows — engineering view. |
| `GET .../config` | Sequence config + **observed** step names per sequence. |

### Write

| Endpoint | Purpose |
|---|---|
| `PUT /api/v2/ems/{line}/{station}/{label}/config` | Save display name, confirmed flag, sequence config. |
| `DELETE /api/v2/ems/{line}/{station}/{label}` | Delete a phantom EM **and its history**. |
| `PUT /api/v2/lines/{line}/schedule` | Replace the weekly shift set. |
| `PUT /api/v2/lines/{line}/availmodel` | Save/clear the line redundancy model. |
| `PUT /api/v2/lines/{line}/stations/{station}/availmodel` | Save/clear a station model. |

Writes are validated server-side (unknown members, `k` out of range, and
duplicate members are rejected with a 400). Sending `{"model": null}`
clears a model back to the default.

### Time windows

Same selector on every historical endpoint — see
[Time windows](#time-windows-every-historical-endpoint) under
**Pulling data for analysis** above. Local midnight uses `APP_TIMEZONE`
(default `America/Chicago`).

---

## Vocabulary

**States** — `productive`, `standby`, `starved`, `blocked`, `paused`,
`down`, `manual`, `offline`, `no_data`.

**Reason types** — `step_fault`, `interlock`, `fault`, `paused`, `flow`.

**Availability** is episode-based: `available / (available + down)`.

- Counted as *available*: productive, standby, starved, blocked, paused.
- Counted as *down*: down only (faults and interlock trips).
- **Excluded from the denominator entirely**: `manual`, `offline`, and any
  time outside the line's production schedule (E10).

So starved/blocked hurt *throughput*, not *availability* — the equipment
could have run. That is deliberate, and it is the thing integrators most
often get wrong.

**Composed availability** is evaluated in the time domain over a k-of-n
tree, never by multiplying percentages: four redundant mags where one is
dead all shift cost the station nothing, because a sibling covered it.
Percentages cannot express that; overlap in time can.

---

## Recipes

These use `jq` to trim the output; drop the pipe to see the full JSON.
On Windows, `curl.exe` (not PowerShell's `curl` alias, which is
`Invoke-WebRequest`).

**What is broken right now?**
```bash
curl -s localhost:8062/api/v2/live | jq '[.[] | select(.state=="down")
  | {line, station, em_label, reason, since}]'
```

**Where did today's downtime go on one line?**
```bash
curl -s "localhost:8062/api/v2/lines/CELL1/summary?window=today" \
  | jq '{availability_pct, top_down_reasons, flow_losses}'
```

**Availability over scheduled production hours only:**
```bash
curl -s "localhost:8062/api/v2/lines/CELL1/composed?window=prod" \
  | jq '{line: .composed.pct, stations, production_min: .composed.production_min}'
```

**What actually made a redundant cell unavailable:**
```bash
curl -s "localhost:8062/api/v2/lines/CELL1/stations/ST34000/composed?window=8h" \
  | jq '.composed.causes'
```

**Live tiles without polling** — subscribe to SSE:
```bash
curl -N localhost:8062/api/v2/stream
```

---

## Conventions and gotchas

- **No authentication.** The hub is unauthenticated and assumes a trusted
  plant network. Anything that can reach port 8062 can read everything and
  call the write endpoints — including `DELETE`, which removes an EM's
  history. Do not expose it beyond the plant network, and prefer MCP for
  agents since that surface is read-only by construction.
- **Ask for the window you mean.** Omitting `window` gives *today*, which
  early in a shift is a very small sample. Percentages over a handful of
  minutes are noise.
- **`null` is not zero.** `availability_pct` is `null` when the denominator
  is empty (no data, or no production scheduled). Render it as "–", never
  as 0%.
- **Unconfirmed EMs are real but unvetted.** Auto-discovered modules appear
  immediately with `confirmed: false` and no sequence config — so they
  record states but no cycles. Check the flag before trusting cycle data.
- **Station and line names are case-insensitive** in paths; `em_label`
  defaults to `main`.
- **Timestamps are RFC3339 UTC.** Durations are milliseconds (`*_ms`) or
  minutes (`*_min` / `minutes`) — the suffix always tells you which.

## Writes are REST-only, on purpose

MCP exposes every *read* endpoint and no write. Because the REST surface is
unauthenticated, keeping the agent-facing side read-only means an agent
cannot save a bad config, clear a schedule, or delete an EM's history —
regardless of what it is asked to do. Keep that property when adding tools.

## Reference

- Endpoint registration: `internal/api/api.go`
- MCP tools: `internal/mcpserv/mcp.go`
- Availability math: `internal/api/handlers.go`, `internal/compose/compose.go`
- Wire protocol (how data gets in): `../plc/TELEMETRY.md`
- Python equipment client: `../clients/python/README.md`
