# emhub API — for agents and integrations

## The design rule

**The math lives in the API.** Availability, cycle statistics, flow losses,
MTTR, and composed availability are all computed server-side. The web UI,
the MCP tools, and `emctl` are thin readers of the same endpoints — so a
bot and a human looking at the same window can never disagree on a number.

Corollary for integrators: **do not recompute.** If you find yourself
averaging percentages or summing minutes client-side, there is almost
certainly an endpoint that already returns the number you want, computed
the way the plant defines it.

Two ways in:

| | Use it for |
|---|---|
| **MCP** (`POST /mcp`) | LLM agents. Tool-shaped, self-describing, read-only. |
| **REST** (`/api/v2/...`) | Scripts, dashboards, anything else. Full surface, including writes. |

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
| `em_downs` | `line`, `station`, `em_label`, `window` | Down episodes with scan-accurate reasons, ack timestamps, reason pareto. |
| `em_cycles` | ″ | Cycle records + stats, interrupted count. |
| `em_steps` | ″ | Step history: name, description, duration, faulted flag. |
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
| `GET /api/v2/ems/{line}/{station}/{label}/intervals` | State timeline. |
| `GET .../steps?limit=` | Step history. |
| `GET .../cycles` | Cycles + stats. |
| `GET .../throughput?bucket=15m\|30m\|1h` | Counts per bucket. |
| `GET .../downs` | Down episodes, raw downs, reason pareto, availability, `production_min`. |
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

Every historical endpoint accepts the same selector:

| Value | Meaning |
|---|---|
| *(omitted)* or `today` | Local midnight → now |
| `8h`, `30m`, `3d` | Rolling duration back from now |
| `prod` | **Today's scheduled production span** for that line |
| `?from=&to=` | Explicit RFC3339 range (`to` defaults to now) |

Local time is `APP_TIMEZONE` (default `America/Chicago`).

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
