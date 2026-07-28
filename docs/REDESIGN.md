# Equipment Monitor v2 — architecture redesign

Status: PROPOSAL (2026-07-22). No backwards compatibility required.

## 1. Backend language — the honest math first

Fleet-wide telemetry load at full rollout: ~120 EMs × (1 Hz heartbeat +
event bursts) ≈ 200–600 datagrams/s, ~1 KB each ≈ under 1 MB/s. Python
asyncio parses tens of thousands of small datagrams per second without
breaking a sweat — **language throughput is not the constraint at this
scale and never will be for one factory.**

The actual scaling bug in the current collector is architectural: tracker
callbacks do *synchronous* psycopg2 round-trips inside the event loop —
every datagram can block the loop on multiple DB writes. At 600/s that
falls over regardless of language.

**Recommendation: Go for the v2 service anyway** — for the right reasons:

- **Deployment**: one static binary + config. No venv, no Python base
  images, trivial to run as a Windows service or tiny container on the
  plant network. This matters more than speed for a SCADA that must stay
  up for months.
- **Concurrency model that prevents the current sin**: ingest goroutine →
  buffered channel → batch writer (COPY every 100 ms or 500 rows). The
  language makes the correct architecture the natural one.
- **Typed wire codec**: the 1108-byte v3 datagram becomes a struct with a
  compile-checked codec instead of hand-maintained offsets.
- **One service serves everything**: ingest + REST API + SSE live stream +
  static frontend + MCP endpoint, one process, one port (+ UDP).

What stays Python: nothing in the hot path. The existing OPC collector
remains as a **migration stopgap** (both write the same DB) until the
telemetry FB is on every PLC, then it retires. OPC enrichment dies with
it — telemetry v3 already carries scan-accurate reasons, which was OPC's
last job.

## 2. Data model — compute at ingest, not at query

Historian pattern: the ingester derives intervals and cycles the moment
events arrive, so every dashboard/API read is a dumb, fast scan.

Keep TimescaleDB. Tables (v2 schema, new database, clean names):

| Table              | Written by | Contents                                       |
|--------------------|-----------|-------------------------------------------------|
| `raw_datagram`     | ingest    | optional ring of raw payloads (debug, 24 h TTL) |
| `step_event`       | ingest    | step transitions w/ duration, faulted           |
| `state_interval`   | ingest    | closed intervals: state, reason_type, reason,   |
|                    |           | ack_ts — ONE table for run/standby/down/paused/ |
|                    |           | blocked/starved/process_wait/manual             |
| `cycle`            | ingest    | cycle_start/complete edges → work_ms,           |
|                    |           | exchange_ms, total_ms, interrupted flag         |
| `mode_interval`    | ingest    | dryCycle / mesBypass / stepMode / ... windows   |
| `operator_event`   | ingest    | resets (more later)                             |
| `em`, `line`, `sequence` | config | hierarchy: line → plc → station → em       |

Continuous aggregates: hourly availability %, cycle stats (p50/p90/mean),
fault counts per em. Dashboards and the compare API read the aggregates;
drill-downs read the raw tables.

Key simplification vs v1: `state_interval` unifies em_availability_raw +
em_runtime_transition + em_down_event + em_flow_event. State classification
(the SEMI E10 + flow deduction logic proven in the Python tracker) moves
into the Go ingester, evaluated per datagram, emitting one interval row per
state change with the reason attached at close.

## 3. Frontend — a real SCADA, not a Dash app

Dash's server-round-trip-per-interaction model is why the current UI feels
laggy; no amount of callback surgery fixes the shape. v2: **React +
TypeScript + Vite SPA**, served by the Go binary, live data over SSE,
charts on ECharts (gantt/heatmap/pareto) + uPlot (dense time series).
Base Power look: Conduit ground, Terminal text, Livewire as the single
interactive accent, Inter, pill interactive elements, 16 px cards — the
existing brand tokens, no dark mode per brand.

Navigation = the physical hierarchy, always drillable, URL-addressable:

```
/                     Site overview — one card per LINE: state strip,
                      availability today, UPH vs takt, active alarms
/line/CELL1           Line view — station strip in line order (the SCADA
                      "mimic"): each EM tile shows state color, current
                      step, dwell timer; flow arrows show blocked/starved
                      direction; timeline gantt of the whole line below
/em/CELL1/ST34000     EM view — tabs:
    /steps            Step history: virtualized table + step duration
                      distribution + step-flow sankey
    /cycles           Cycle time: trend w/ p50/p90 bands, work vs exchange
                      split, cycle table with drill-to-steps
    /availability     E10 stacked timeline + pareto of down reasons +
                      MTTR split (response vs repair) + MTBF trend
    /alarms           Alarm/fault history: every down event + fault event
                      w/ scan-accurate reasons, filter/search/export
```

Everything above the EM level is aggregation of the same intervals, so the
overview is cheap and always consistent with the drill-down.

## 4. Agent interface — API-first, MCP on top

Design rule: **the math lives in the API, the narration lives in the
agent.** Every number the UI shows comes from a JSON endpoint; the agent
uses the same endpoints, so human and agent never disagree.

- REST: `/api/v2/...` — lines, ems, intervals, cycles, faults, aggregates,
  all with `from`/`to` windows.
- The flagship endpoint: `GET /api/v2/compare?a=CELL1&b=CELL2&window=today`
  returns a **decomposed delta**: availability split by state with minutes,
  throughput (cycles + interrupted count), cycle p50/p90 work vs exchange,
  top-5 down reasons per side with minutes, flow losses by station with
  waiting-on reasons, mode context (dry-cycle/bypass minutes excluded),
  MTTR response/repair split. Everything an agent needs to answer "why is
  Line 1 better than Line 2 today" in one call, structured for citation.
- **MCP server** (HTTP transport, same binary): tools `list_lines`,
  `line_summary`, `compare_lines`, `em_detail`, `down_events`,
  `fault_pareto`, `cycle_stats`, `step_history`, `flow_losses`. Thin
  wrappers over the REST endpoints with good descriptions — Claude/agents
  connect directly.
- **CLI** (`emctl`): same client, human-shaped —
  `emctl compare CELL1 CELL2 --today`, `emctl em ST34000 --line CELL1
  --availability --last 8h`, `--json` for scripts.

## 5. Migration

1. **Phase 1 — Go ingest + schema v2**: UDP v3 ingest, state machine port
   (classification logic already proven in Python — port with the Python
   test datagrams as golden tests), batch writer, runs alongside v1.
2. **Phase 2 — API + MCP + CLI** on the v2 schema.
3. **Phase 3 — React frontend**, feature-parity with the four EM tabs +
   line/site overviews; v1 Dash stays up until sign-off.
4. **Phase 4 — retire** Python collector + Dash once every PLC has the FB.

Risks: gopcua not needed (OPC retired, stopgap stays Python); PLC clock
skew handling ports as-is; the SIM1 station is the end-to-end test rig for
the whole pipeline before any real PLC is on v2.
