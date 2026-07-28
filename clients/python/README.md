# emtel — Python equipment telemetry

## Why

Some equipment on the line is driven by Python scripts rather than a PLC.
We want their state and steps captured exactly like the PLC EMs — same
timeline, same availability math, same paretos — without a second ingest
path to build and maintain.

The collector's contract is not "a PLC"; it is **the wire format**. Any
process that sends a valid v4 datagram to UDP `:15020` is indistinguishable
from `EquipmentModuleTelemetry` downstream. So `emtel` is a small client
that speaks that format from Python. **No backend changes.**

`emtel.py` is a single standard-library file — copy it next to your script.
No pip, no venv, no dependencies (works on locked-down equipment PCs).

## Quickstart

```python
from emtel import EquipmentModule

em = EquipmentModule(line="MOD1", station="ST52000", em_label="main",
                     collector=("10.0.1.50", 15020))

with em:                                        # heartbeat starts; EM = standby
    while True:
        with em.step("10", "Home axes"):
            home_axes()
        with em.step("20", "Load part"):
            with em.waiting_on("Infeed conveyor part present"):
                await_infeed_part()             # -> starved (per UI config)
            clamp_part()
        with em.step("30", "Vision inspect"):
            run_vision()                        # a raise here -> step fault
```

See `example_station.py` for a complete instrumented cell.

Verify connectivity from the equipment PC before instrumenting anything:

```bash
python emtel.py --selftest --host 10.0.1.50 --line TEST --station ST99000
```

It sends a short burst (steps, a wait, an injected fault) and reports
`sent=` / `errors=`. The EM then appears in the SCADA as **unconfirmed**,
same as a new PLC EM.

## What each call produces

| Call | Recorded state |
|---|---|
| `with em:` (idle, no step running) | `standby` — available, not producing |
| `with em.step(...)` | `productive` |
| `with em.waiting_on(...)` inside a **configured** starved/blocked step | `starved` / `blocked` |
| `with em.waiting_on(...)` on any other step | stays `productive` |
| exception inside `em.step(...)` | `down`, reason type `step_fault` |
| `em.fault("Vision timeout", conditions="Part present not made")` | `down` — UI shows `alarm — conditions` |
| `em.interlock_fail("Guard door open")` | `down`, reason type `interlock` |
| `em.pause()` | `paused` — counts as available |
| `em.manual()` | `manual` — excluded from the availability denominator |
| process exits cleanly | final `standby`, then `offline` after ~10 s |
| process crashes | `down` with the exception, then `offline` |

## Configure it in the UI

Steps are the join key between your script and the hub. After the EM
registers, open its **Config** tab and fill in — using the exact step names
your code passes:

- **cycle start / cycle complete step** — enables cycle time and throughput
- **starved steps / blocked steps** — makes `waiting_on` count as a flow loss

Nothing is inferred. A `waiting_on` at a step that is in neither list stays
productive, so an unconfigured EM never invents downtime.

Once configured, everything else follows automatically: E10 availability
against the line's production schedule, composed k-of-n redundancy models
(a Python station can be a leaf alongside PLC EMs), alarm paretos, MTTR.

## Rules the client follows

- **Telemetry never breaks production code.** Every send is wrapped; errors
  are counted on `em.send_errors` / `em.last_error`, never raised. The
  heartbeat is a daemon thread, so it can't block process exit.
- **1 Hz heartbeat.** The collector marks an EM offline after 10 s of
  silence; 1 Hz gives 10x margin. State changes send immediately, so the
  timeline is edge-accurate, not sampled.
- **Truncation is safe.** Field limits are step 60, description /
  alarm / conditions / waiting-on 200, interlock 160, station / line 32,
  em_label 16. Longer strings are cut, not rejected. Text is latin-1;
  non-encodable characters become `?`.

## Gotchas

- **Keep step names stable.** Renaming a step orphans its history and
  silently breaks cycle detection — the same discipline as the PLC.
- **Clock skew.** The hub trusts your timestamp within ±120 s and falls
  back to receive time beyond that. Keep equipment PCs on NTP.
- **Short-lived scripts** show `standby` for ~10 s after exit, then
  `offline`. Offline is excluded from the availability denominator, so idle
  time between runs neither helps nor hurts the number. If you want the gap
  to read as genuine idle time instead, keep one long-lived process and
  call `em.standby()` between jobs.
- **One instance per EM.** A script driving several independent modules
  should create several `EquipmentModule` objects; each heartbeats on its
  own thread with its own identity.
- **Pick the line name deliberately.** `line` is what the hub routes on
  (NAT-proof, survives re-IP). It must match the real line the equipment
  belongs to, or the EM lands in its own phantom line.

## Reference

- Wire format and field offsets: `../../plc/TELEMETRY.md`
- Decoder (the authority): `../../hub/internal/wire/wire.go`
- State classification rules: `../../hub/internal/tracker/tracker.go`
