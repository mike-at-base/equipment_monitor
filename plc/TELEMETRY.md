# EM Telemetry — PLC push over UDP

## Why

OPC UA subscriptions are pull-based: the S7-1500 samples monitored items on
its `MinimumSamplingInterval` (typically 1000 ms, revised publish 500 ms).
Any step or signal that changes faster than that window is silently lost —
the collector sees the value before and the value after, never the
transition. That corrupts step history for fast steps and can drop fault
edges entirely.

`EquipmentModuleTelemetry` inverts the flow: the PLC pushes a small UDP
datagram **from the scan cycle** the moment anything changes. Nothing can
change faster than a scan, and the FB queues snapshots when several changes
land on consecutive scans, so no transition is ever skipped.

## Why not CoAP

CoAP was considered — it is UDP-based and its "observe" model is exactly
this push pattern. But CoAP's value is *interoperability with third-party
IoT devices*: implementing its message layer (tokens, option encoding,
observe registration, CON/ACK retransmission) in SCL is significant code
for zero benefit when we control both endpoints. This design borrows what
makes CoAP fast — small connectionless datagrams, event-driven push — and
covers reliability the pragmatic way:

- **Sequence counter** per EM: the collector detects gaps (lost datagrams).
- **Heartbeat snapshot** every `heartbeatInterval` (default 1 s): any lost
  event self-heals within a second, and the collector gets a liveness
  signal per EM.
- **OPC UA stays connected** for the on-demand enrichment reads (permissive
  condition details) and as a full fallback if telemetry is down.

## Wire format

One UDP datagram per event/heartbeat produced by `Serialize` of
`typeEquipmentModuleTelemetry` (S7 standard/non-optimized layout, big-endian,
strings as `[max][len][chars...]`).  Wire version: **5** — 1406 bytes.

The layout is append-only: every v5 offset ≤ 1141 is byte-identical to v4, so
a mixed fleet works unchanged. The collector's **minimum** accepted version
stays 4 — a v4 PLC (1142 bytes) keeps reporting after the collector is
upgraded, it simply carries no branch attribution. A v3 PLC (no `lineName`,
1108 bytes) is routed by source IP; v4+ is routed by `lineName`, which is
NAT-proof and enables auto-registration.

| Offset | Type        | Field           | Notes                                   |
|-------:|-------------|-----------------|------------------------------------------|
| 0      | USInt       | version         | 3 (legacy), 4, or 5                      |
| 1      | USInt       | msgType         | 1 = event, 2 = heartbeat                 |
| 2      | Word        | statusBits      | see bit map below                        |
| 4      | Word        | modeBits        | see bit map below                        |
| 6      | Int         | activeSequence  | 0 = none                                 |
| 8      | UDInt       | seq             | wraps at 2^32                            |
| 12     | Time        | stepActiveTime  | ms in current step                       |
| 16     | LDT         | plcTime         | ns since 1970-01-01 UTC                  |
| 24     | String[32]  | stationName     | e.g. `ST34000`                           |
| 58     | String[16]  | emLabel         | e.g. `main`, `SL01_MAG01`                |
| 76     | String[60]  | step            | active sequence's current step           |
| 138    | String[200] | stepDescription |                                          |
| 340    | String[200] | alarmMessage    | `status.alarm.message` (composed)        |
| 542    | String[160] | interlockFails  | ALL failing interlock conditions,        |
|        |             |                 | `'; '`-separated, first-out first        |
| 704    | String[200] | faultConditions | failing step-permissive conditions,      |
|        |             |                 | latched on the fault scan                |
| 906    | String[200] | waitingOn       | failing permissives of a healthy dwell   |
|        |             |                 | past dwellCaptureDelay (live flow reason)|
| 1108   | String[32]  | lineName        | v4+: line/site id the PLC declares       |
|        |             |                 | (e.g. `MOD1`); collector routes on it    |
| 1142   | String[60]  | branchTaken     | v5+: `nextStep` of the branch that       |
|        |             |                 | actually satisfied; `''` until resolved  |
| 1204   | String[200] | dwellReason     | v5+: that branch's unmet conditions —    |
|        |             |                 | the attributable flow reason (see below) |

`statusBits`: bit0 automatic · bit1 fault · bit2 running · bit3 paused ·
bit4 stopped · bit5 unknown · bit6 stepFaulted · bit7 interlockOk ·
bit8 extAlarmActive · bit9 reset.

`modeBits`: bit0 idle · bit1 stepMode · bit2 mesBypass · bit3 dryCycle ·
bit4 endOfCycle · bit5 pauseAtHome · bit6 requestEntry.

### What the collector does with the extras

- **modeBits** → `em_mode_event` rows on change: history can exclude
  dry-cycle / MES-bypass windows from OEE and treat step-mode time as
  maintenance activity.
- **reset (bit9)** → `em_operator_event` rows; the FIRST reset while a
  down event is open stamps `em_down_event.ack_ts`, splitting MTTR into
  response time (down → ack) and repair time (ack → recovered).
- **interlockFails** → down-event reasons carry every failing condition,
  not just the first-out.
- **waitingOn** → deduced flow states.  No blocked/starved bits exist in
  the PLC (they would never be maintained); instead, a healthy dwell past
  `dwellCaptureDelay` (FB input, default 10 s) streams the active step's
  failing permissives, and the collector classifies the wait:
    1. explicit config step lists (override, legacy)
    2. cycle position — dwelling AT `cycle_start_step` = **starved**;
       dwelling in the exchange phase after `cycle_complete_step` =
       **blocked**; mid-cycle = **process_wait** (charged to the station)
    3. direction keywords in the permissive text ("part present",
       "infeed" → starved; "downstream", "clear", "occupied" → blocked)
  The permissive descriptions are the reason text — self-maintaining,
  because operators see them on the HMI.  `cycle_complete_step` is an
  optional per-sequence key in config.yaml next to `cycle_start_step`.

### Fault reason semantics

`faultConditions` is latched by the FB **on the same scan the step fault
rises** — every enabled branch's permissive conditions with `ok = FALSE`
at that instant, `'; '`-joined and de-duplicated. One scan later would
already be too late (a stop/reset overwrites `activeStepBranch`), which is
exactly why after-the-fact OPC reads were unreliable.

The collector composes the down-event / fault-event reason as:

- permissive-driven fault (e.g. timeout waiting on a condition):
  `"<alarmMessage> — <faultConditions>"` → `"Unclamp Timeout — Clamp
  retracted not made"`
- external device fault (no failing permissives): `alarmMessage` alone →
  `"Robot fault: servo alarm SRVO-062"` (the external message passes
  through `status.alarm.message` on the PLC)

This telemetry-sourced reason is authoritative: the slower OPC enrichment
read is skipped for it.

## Branch attribution (v5)

`waitingOn` is the **union** of unmet conditions across every *enabled*
branch of the active step. On a step with one branch that is exactly what
you want. On a step with several it is misleading, because a step's
transition is an OR of ANDs:

```
advance = (B0.c0 AND B0.c1 …) OR (B1.c0 AND B1.c1 …)
```

The branch that is *not* taken has a discriminator that stays false for the
whole dwell — if it were true the sequencer would already have jumped and
there'd be no dwell to report. So it always appears in the union. Real
example, CELL1 ST12000 step 240:

| Branch | Conditions | During the wait |
|---|---|---|
| → skip | dispense workstate **complete** | false → in the union ❌ |
| → 250 | workstate **not** complete, 7 stacks, 8 stacks | stacks false → in the union ✓ |

No structural rule fixes this. "Fewest unmet conditions" picks the skip
branch (1 unmet vs 2) — the wrong one. A discriminator is indistinguishable
from a real wait unless the PLC is told which is which, and a tagging
convention only works if every engineer follows it forever.

So the FB waits until the machine answers the question itself. It latches
each branch's unmet conditions as a bitmask during the dwell, and on the
scan a branch's `permissive.status.ok` goes true — the branch the sequencer
is about to take — it materializes `dwellReason` from that branch's latched
mask and sets `branchTaken`. That happens one scan *before* the transition
overwrites `activeStepBranch` with the next step's branches, the same race
that makes after-the-fact OPC reads useless.

The collector then closes the flow interval with `dwellReason` instead of
`waitingOn`. Consequences worth knowing:

- **`waitingOn` is unchanged** and still the live reason. Attribution is
  retroactive — it lands when the wait ends.
- A dwell that never ends (a genuine ongoing stoppage) has no branch to
  attribute to, so it keeps the union text.
- A step forced or jumped without a branch satisfying leaves both fields
  empty, and the union stands. Nothing is invented.

## PLC integration (per EM)

1. Import `typeEquipmentModuleTelemetry.udt` and
   `EquipmentModuleTelemetry.scl` into the project library.
2. Drop one instance next to each EM call, AFTER the EquipmentModule FB in
   the scan so it sees the freshest data:

```scl
"ST34000_Telemetry_DB"(
    stationName    := 'ST34000',
    emLabel        := 'main',
    hwInterfaceId  := 64,              // HW id of the PN interface
    connectionId   := W#16#0301,       // unique per instance, per CPU
    localPort      := 15301,           // unique per instance, per CPU
    remoteIp1      := 10,  remoteIp2 := 200,
    remoteIp3      := 2,   remoteIp4 := 226,   // collector host
    remotePort     := 15020,
    controlInterface := "ST34000_Station_DB".mainEquipmentModule
);
```

Each instance owns one UDP "connection" (a local port binding — no session
state). Give every instance on a CPU a unique `connectionId` and
`localPort`; a simple scheme is `15300 + n`.

## Collector integration

Enable in `config.yaml`:

```yaml
telemetry:
  enabled: true
  listen_port: 15020
```

The receiver maps datagrams to equipment modules by
`(source IP, stationName, emLabel)` — source IP must match the host of the
PLC's `opc_endpoint`. Events feed the same `EMStateTracker` callbacks as
OPC notifications (trackers dedupe, so running both sources is safe; the
faster one wins). Sequence gaps and stale PLC clocks are logged.
