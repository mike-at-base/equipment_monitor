import { useState } from "react";
import { Link } from "react-router-dom";
import { api, LiveEM, STATE_LABEL, stateColor } from "../api";
import { ErrorBox, Loading, usePolledAsync } from "../components/ui";

const DEFAULT_LINE = "SIM";
const DEFAULT_SPEC =
  "ST34000:main,ROB01,ROB02,MAG01,MAG02,MAG03,MAG04,MAG05,MAG06,MAG07,MAG08";

const SIM_STATES: { key: string; label: string; hint: string }[] = [
  { key: "up", label: "Up", hint: "automatic + running → productive" },
  { key: "down", label: "Down", hint: "faulted → unavailable" },
  { key: "standby", label: "Standby", hint: "in auto, idle — counts as available" },
  { key: "paused", label: "Paused", hint: "operator pause — counts as available" },
  { key: "manual", label: "Manual", hint: "out of automatic — composition-down" },
  { key: "offline", label: "Offline", hint: "stops reporting → telemetry lost" },
];

// Training simulator: drive fake EM states from the browser and watch the
// line, station RBD, and availability math react. Telemetry enters through
// the real UDP collector — nothing downstream knows it's simulated.
export default function Sim({ live }: { live: LiveEM[] }) {
  const sim = usePolledAsync(() => api.sim(), [], 2000);
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<unknown>();

  if (sim.err) return <ErrorBox err={sim.err} />;
  if (!sim.data) return <Loading />;
  const s = sim.data;

  const act = (fn: () => Promise<unknown>) => {
    setBusy(true); setErr(undefined);
    fn().catch(setErr).finally(() => setBusy(false));
  };

  const liveByKey: Record<string, LiveEM | undefined> = {};
  for (const e of live) {
    if (e.line === s.line) liveByKey[`${e.station}/${e.em_label}`.toLowerCase()] = e;
  }

  return (
    <>
      <div className="card" style={{ marginTop: 16 }}>
        <h2>Availability simulator</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Drives a sandbox line through the real telemetry pipeline so you can
          learn how states, redundancy models, and availability interact. Try
          it: open the station page, fault one MAG (nothing happens to the
          station), then fault all four (the station goes down).
        </p>
        {!s.running && <SetupForm busy={busy} onStart={(l, sp) => act(() => api.simStart(l, sp))} />}
        {s.running && (
          <div className="sim-toolbar">
            <span>Simulating line <b>{s.line}</b> · {s.ems.length} EMs</span>
            <Link className="linkbtn" to={`/line/${s.line}`}>line view →</Link>
            {[...new Set(s.ems.map((e) => e.station))].map((st) => (
              <Link key={st} className="linkbtn" to={`/line/${s.line}/station/${st}`}>{st} RBD →</Link>
            ))}
            <span className="spacer" />
            <button disabled={busy} onClick={() => act(() => api.simState("*", "up"))}>All up</button>
            <button disabled={busy} onClick={() => act(() => api.simStop(false))}>Stop</button>
            <button className="btn-danger" disabled={busy}
                    title="stop and delete the sandbox line's EMs + history"
                    onClick={() => act(() => api.simStop(true))}>Stop &amp; delete sandbox</button>
          </div>
        )}
        {err != null && <p style={{ color: "var(--st-down)" }}>{String(err)}</p>}
      </div>

      {s.running && (
        <>
          <div className="card">
            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <span className="muted">Fault reason used by Down clicks:</span>
              <input className="sim-reason" value={reason} placeholder="Simulated fault"
                     onChange={(e) => setReason(e.target.value)} />
            </div>
          </div>
          {[...new Set(s.ems.map((e) => e.station))].map((st) => (
            <div className="card" key={st}>
              <h2>{st}</h2>
              <div className="sim-grid">
                {s.ems.filter((e) => e.station === st).map((e) => {
                  const lv = liveByKey[`${e.station}/${e.em_label}`.toLowerCase()];
                  const obs = lv?.state ?? "no_data";
                  return (
                    <div className="sim-row" key={e.em_label}>
                      <span className="sim-name">{e.em_label}</span>
                      <span className="chip" style={{ background: stateColor(obs) }}
                            title={`observed by the tracker${lv?.reason ? `: ${lv.reason}` : ""}`}>
                        {STATE_LABEL[obs] ?? obs}
                      </span>
                      <span className="sim-btns">
                        {SIM_STATES.map((st2) => (
                          <button key={st2.key} title={st2.hint} disabled={busy}
                                  className={e.state === st2.key ? "active" : ""}
                                  onClick={() => act(() => api.simState(
                                    `${e.station}/${e.em_label}`, st2.key,
                                    st2.key === "down" ? reason : ""))}>
                            {st2.label}
                          </button>
                        ))}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </>
      )}
    </>
  );
}

function SetupForm({ busy, onStart }: {
  busy: boolean; onStart: (line: string, spec: string) => void;
}) {
  const [line, setLine] = useState(DEFAULT_LINE);
  const [spec, setSpec] = useState(DEFAULT_SPEC);
  return (
    <div className="sim-setup">
      <label>Sandbox line name
        <input value={line} onChange={(e) => setLine(e.target.value)} />
      </label>
      <label>Topology — STATION:em,em;STATION:em,…
        <textarea rows={3} value={spec} onChange={(e) => setSpec(e.target.value)} />
      </label>
      <p className="muted" style={{ margin: "4px 0" }}>
        The default is a redundant cell: 1 main + 2 robots + 4 mags each.
        After starting, configure its availability model on the station page
        (e.g. mags as "require ANY").
      </p>
      <button className="btn-primary" disabled={busy || !line.trim()}
              onClick={() => onStart(line.trim(), spec)}>Start simulator</button>
    </div>
  );
}
