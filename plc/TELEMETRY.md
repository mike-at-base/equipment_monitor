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

One UDP datagram per event/heartbeat, 622 bytes, produced by `Serialize`
of `typeEquipmentModuleTelemetry` (S7 standard/non-optimized layout,
big-endian, strings as `[max][len][chars...]`):

| Offset | Type        | Field              | Notes                                  |
|-------:|-------------|--------------------|----------------------------------------|
| 0      | USInt       | version            | 1                                      |
| 1      | USInt       | msgType            | 1 = event, 2 = heartbeat               |
| 2      | Word        | statusBits         | see bit map below                      |
| 4      | UDInt       | seq                | wraps at 2^32                          |
| 8      | LDT         | plcTime            | ns since 1970-01-01 UTC                |
| 16     | Int         | activeSequence     | 0 = none                               |
| 18     | Time        | stepActiveTime     | ms in current step                     |
| 22     | String[32]  | stationName        | e.g. `ST34000`                         |
| 56     | String[16]  | emLabel            | e.g. `main`, `SL01_MAG01`              |
| 74     | String[60]  | step               | active sequence's current step         |
| 136    | String[200] | stepDescription    |                                        |
| 338    | String[200] | alarmMessage       | `status.alarm.message` (composed)      |
| 540    | String[80]  | interlockFirstFail | first failing interlock condition desc |

`statusBits`: bit0 automatic · bit1 fault · bit2 running · bit3 paused ·
bit4 stopped · bit5 unknown · bit6 stepFaulted · bit7 interlockOk ·
bit8 extAlarmActive.

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
