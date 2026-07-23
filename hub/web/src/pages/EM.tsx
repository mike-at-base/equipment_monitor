import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useParams } from "react-router-dom";
import { api, CycleRow, fmtClock, fmtMs, fmtSince, stateColor, STATE_LABEL, STATE_ORDER } from "../api";
import { Bars, ErrorBox, Gantt, Loading, StateChip, Trend, useAsync, useNow, useWindow, VBars } from "../components/ui";

// EM drill-down: Steps / Cycles / Availability / Alarms
export default function EMPage() {
  const { line = "", station = "", label = "" } = useParams();
  const tabs = [
    { path: "steps", name: "Step history" },
    { path: "cycles", name: "Cycle time" },
    { path: "availability", name: "Availability" },
    { path: "alarms", name: "Alarm history" },
    { path: "debug", name: "Raw / debug" },
  ];
  return (
    <>
      <div className="tabs">
        {tabs.map((t) => (
          <NavLink key={t.path} to={t.path}
            className={({ isActive }) => (isActive ? "active" : "")}>{t.name}</NavLink>
        ))}
      </div>
      <Routes>
        <Route path="steps" element={<Steps l={line} s={station} e={label} />} />
        <Route path="cycles" element={<Cycles l={line} s={station} e={label} />} />
        <Route path="availability" element={<Availability l={line} s={station} e={label} />} />
        <Route path="alarms" element={<Alarms l={line} s={station} e={label} />} />
        <Route path="debug" element={<Debug l={line} s={station} e={label} />} />
        <Route path="*" element={<Steps l={line} s={station} e={label} />} />
      </Routes>
    </>
  );
}

type P = { l: string; s: string; e: string };

function Steps({ l, s, e }: P) {
  const { win } = useWindow();
  const q = useAsync(() => api.steps(l, s, e, win, 800), [l, s, e, win]);
  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;
  const steps = q.data;

  // per-step aggregate for the summary bars
  const agg: Record<string, { total: number; n: number }> = {};
  for (const st of steps) {
    agg[st.step] = agg[st.step] ?? { total: 0, n: 0 };
    agg[st.step].total += st.duration_ms;
    agg[st.step].n++;
  }
  const avg = Object.entries(agg)
    .map(([step, a]) => ({ step, avg: a.total / a.n, n: a.n }))
    .sort((a, b) => b.avg - a.avg)
    .slice(0, 12);

  return (
    <>
      <div className="card">
        <h2>Average step duration ({win})</h2>
        <Bars rows={avg.map((r) => ({
          name: `${r.step} (×${r.n})`, value: r.avg / 1000, suffix: " s",
          color: "var(--grounded)",
        }))} />
      </div>
      <div className="card">
        <h2>Step history · {steps.length} events</h2>
        <div className="tablewrap">
          <table className="data">
            <thead><tr>
              <th>Time</th><th>Seq</th><th>Step</th><th>Description</th>
              <th style={{ textAlign: "right" }}>Duration</th><th>Faulted</th>
            </tr></thead>
            <tbody>
              {steps.map((st, i) => (
                <tr key={i}>
                  <td className="num">{fmtClock(st.start_ts)}</td>
                  <td className="num">{st.seq_index}</td>
                  <td className="num">{st.step}</td>
                  <td>{st.description}</td>
                  <td className="num">{fmtMs(st.duration_ms)}</td>
                  <td>{st.was_faulted ? <span className="chip" style={{ background: "var(--st-down)" }}>fault</span> : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

function Cycles({ l, s, e }: P) {
  const { win } = useWindow();
  const q = useAsync(() => api.cycles(l, s, e, win), [l, s, e, win]);
  const [selTs, setSelTs] = useState<string>();
  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;
  const { stats, cycles } = q.data;
  const points = [...cycles].reverse().map((c) => ({ t: Date.parse(c.start_ts), v: c.total_ms }));
  const selected = cycles.find((c) => c.start_ts === selTs) ?? cycles[0];
  return (
    <>
      <div className="tiles" style={{ marginTop: 16 }}>
        <T label="Cycles" v={`${stats.count}`} />
        <T label="Per hour" v={stats.per_hour != null ? `${stats.per_hour}` : "–"} />
        <T label="p50" v={fmtMs(stats.p50_ms)} />
        <T label="p90" v={fmtMs(stats.p90_ms)} />
        <T label="Work avg" v={fmtMs(stats.work_avg_ms)} />
        <T label="Exchange avg" v={fmtMs(stats.exchange_avg_ms)} />
        <T label="Interrupted" v={`${stats.interrupted}`} />
      </div>
      <div className="card">
        <h2>Cycle time trend ({win})</h2>
        <Trend points={points} unit="s" />
      </div>
      <Throughput l={l} s={s} e={e} />
      {selected && (
        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
            <h2 style={{ margin: 0 }}>Step waterfall · cycle at {fmtClock(selected.start_ts)}</h2>
            <span className="muted" style={{ fontSize: 13 }}>
              {fmtMs(selected.total_ms)} total
              {selected.interrupted && <> · <span style={{ color: "var(--st-down)" }}>interrupted</span></>}
              {" · pick a row below"}
            </span>
          </div>
          <CycleWaterfall l={l} s={s} e={e} cyc={selected} />
        </div>
      )}
      <div className="card">
        <h2>Cycles</h2>
        <div className="tablewrap">
          <table className="data">
            <thead><tr>
              <th>Start</th><th style={{ textAlign: "right" }}>Total</th>
              <th style={{ textAlign: "right" }}>Work</th>
              <th style={{ textAlign: "right" }}>Exchange</th><th>Interrupted</th>
            </tr></thead>
            <tbody>
              {cycles.map((c, i) => (
                <tr key={i} onClick={() => setSelTs(c.start_ts)}
                    className={c.start_ts === selected?.start_ts ? "sel" : "clickable"}>
                  <td className="num">{fmtClock(c.start_ts)}</td>
                  <td className="num">{fmtMs(c.total_ms)}</td>
                  <td className="num">{fmtMs(c.work_ms)}</td>
                  <td className="num">{fmtMs(c.exchange_ms)}</td>
                  <td>{c.interrupted ? <span className="chip" style={{ background: "var(--st-blocked)" }}>yes</span> : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

const BUCKETS = ["15m", "30m", "1h"];

// Completed-cycle counts over the window, bucketed by a user-chosen width
// (default 1h). Actual counts, not a rate.
function Throughput({ l, s, e }: P) {
  const { win } = useWindow();
  const [bucket, setBucket] = useState("1h");
  const q = useAsync(() => api.throughput(l, s, e, win, bucket), [l, s, e, win, bucket]);
  const bars = (q.data?.buckets ?? []).map((b) => ({
    t: Date.parse(b.bucket_ts), v: b.count,
    label: fmtClock(b.bucket_ts).replace(/:\d\d /, " "),
  }));
  return (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
        <h2 style={{ margin: 0 }}>Throughput ({win})</h2>
        <div className="winpick" role="group" aria-label="bucket size">
          {BUCKETS.map((b) => (
            <button key={b} className={b === bucket ? "active" : ""} onClick={() => setBucket(b)}>{b}</button>
          ))}
        </div>
      </div>
      <p className="muted" style={{ margin: "4px 0 0" }}>Completed cycles per {bucket} bucket.</p>
      {q.err ? <ErrorBox err={q.err} />
        : !q.data ? <Loading />
        : <VBars bars={bars} unit="" />}
    </div>
  );
}

// Per-step waterfall for one cycle: each step is a bar positioned at its
// offset from cycle start, width = its duration. Reveals which step ate the
// cycle time; the step that was active during a fault carries that time.
function CycleWaterfall({ l, s, e, cyc }: P & { cyc: CycleRow }) {
  const q = useAsync(() => api.stepsRange(l, s, e, cyc.start_ts, cyc.end_ts),
    [l, s, e, cyc.start_ts, cyc.end_ts]);
  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;
  const t0 = Date.parse(cyc.start_ts);
  const total = Math.max(cyc.total_ms, 1);
  const steps = q.data
    .filter((st) => st.seq_index === cyc.seq_index)
    .sort((a, b) => Date.parse(a.start_ts) - Date.parse(b.start_ts));
  if (!steps.length) return <div className="empty">No step detail retained for this cycle.</div>;
  const longest = Math.max(...steps.map((st) => st.duration_ms));
  return (
    <div className="waterfall">
      {steps.map((st, i) => {
        const offset = Math.max(0, Date.parse(st.start_ts) - t0);
        const leftPct = Math.min(100, (100 * offset) / total);
        const widthPct = Math.min(100 - leftPct, (100 * st.duration_ms) / total);
        const color = st.was_faulted ? "var(--st-down)"
          : st.duration_ms === longest ? "var(--st-starved)" : "var(--grounded)";
        return (
          <div className="row" key={i} title={st.description}>
            <span className="name">{st.step} · {st.description}</span>
            <div className="track">
              <div className="seg" style={{ left: `${leftPct}%`, width: `${Math.max(widthPct, 0.4)}%`, background: color }} />
            </div>
            <span className="val">{fmtMs(st.duration_ms)}</span>
          </div>
        );
      })}
    </div>
  );
}

function Availability({ l, s, e }: P) {
  const { win } = useWindow();
  const iv = useAsync(() => api.intervals(l, s, e, win), [l, s, e, win]);
  const downs = useAsync(() => api.downs(l, s, e, win), [l, s, e, win]);
  if (iv.err) return <ErrorBox err={iv.err} />;
  if (!iv.data || !downs.data) return <Loading />;

  const now = Date.now();
  const spanMs: Record<string, number> = {};
  let from = now;
  for (const i of iv.data) {
    const st = Date.parse(i.start_ts), en = Date.parse(i.end_ts);
    from = Math.min(from, st);
    spanMs[i.state] = (spanMs[i.state] ?? 0) + (en - st);
  }
  // availability + MTTR come from the API (episode-based: retry blips and
  // gate inter-states inside a downtime episode are NOT uptime)
  const eps = downs.data.episodes;
  const epMin = eps.reduce((a, e) => a + e.minutes, 0);
  const acked = eps.filter((e) => e.response_min != null);

  return (
    <>
      <div className="tiles" style={{ marginTop: 16 }}>
        <T label={`Availability (${win})`}
           v={downs.data.availability_pct != null ? `${downs.data.availability_pct.toFixed(1)}%` : "–"} />
        <T label="Down episodes" v={`${eps.length}`} />
        <T label="Down minutes" v={epMin.toFixed(1)} />
        <T label="Retries" v={`${eps.reduce((a, e) => a + e.retries, 0)}`} />
        <T label="Avg response" v={acked.length ? `${(acked.reduce((a, d) => a + d.response_min!, 0) / acked.length).toFixed(1)} min` : "–"} />
        <T label="Avg repair" v={acked.length ? `${(acked.reduce((a, d) => a + d.repair_min!, 0) / acked.length).toFixed(1)} min` : "–"} />
      </div>
      <div className="card">
        <h2>State timeline ({win})</h2>
        <Gantt rows={[{ label: s, intervals: iv.data }]} from={from} to={now} />
      </div>
      <div className="card">
        <h2>Time by state</h2>
        <Bars rows={STATE_ORDER.filter((st) => spanMs[st])
          .map((st) => ({ name: STATE_LABEL[st], value: spanMs[st] / 60000, color: stateColor(st) }))} />
      </div>
      {downs.data.top_reasons.length > 0 && (
        <div className="card">
          <h2>Down reasons</h2>
          <Bars rows={downs.data.top_reasons.map((r) => ({
            name: `${r.reason} (×${r.count})`, value: r.minutes, color: "var(--st-down)",
          }))} />
        </div>
      )}
    </>
  );
}

function Alarms({ l, s, e }: P) {
  const { win } = useWindow();
  const q = useAsync(() => api.downs(l, s, e, win), [l, s, e, win]);
  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;
  const { episodes } = q.data;
  if (!episodes.length) return <div className="empty">No downtime episodes in this window. 🎉</div>;
  return (
    <div className="card">
      <h2>Downtime episodes · {episodes.length}</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        Root cause is latched at the first fault; gate openings, resets, and
        retry attempts inside an episode are absorbed (see the Availability
        timeline for every raw state).
      </p>
      <div className="tablewrap">
        <table className="data">
          <thead><tr>
            <th>Start</th><th>Type</th><th>Root cause</th><th>Step</th>
            <th style={{ textAlign: "right" }}>Duration</th>
            <th style={{ textAlign: "right" }}>Retries</th>
            <th style={{ textAlign: "right" }}>Response</th>
            <th style={{ textAlign: "right" }}>Repair</th>
            <th></th>
          </tr></thead>
          <tbody>
            {episodes.map((d, i) => (
              <tr key={i}>
                <td className="num">{fmtClock(d.start_ts)}</td>
                <td>{d.reason_type}</td>
                <td>{d.reason}</td>
                <td className="num">{d.step_name}</td>
                <td className="num">{d.minutes.toFixed(1)} min</td>
                <td className="num">{d.retries || ""}</td>
                <td className="num">{d.response_min != null ? `${d.response_min.toFixed(1)} min` : "–"}</td>
                <td className="num">{d.repair_min != null ? `${d.repair_min.toFixed(1)} min` : "–"}</td>
                <td>{d.ongoing ? <span className="chip" style={{ background: "var(--st-down)" }}>ongoing</span> : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// bits whose "on" state is bad (red) vs good (green); the rest are neutral
const BAD_BITS = new Set(["fault", "step_fault", "ext_alarm", "unknown"]);
const GOOD_BITS = new Set(["automatic", "running", "interlock_ok"]);

function BitChip({ name, on, kind }: { name: string; on: boolean; kind: "status" | "mode" }) {
  let cls = "bitchip";
  if (on) {
    cls += " on";
    if (kind === "status" && BAD_BITS.has(name)) cls += " bad";
    else if (kind === "status" && GOOD_BITS.has(name)) cls += " good";
  }
  return <span className={cls}>{name}</span>;
}

function KV({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className={mono ? "v mono" : "v"}>{v || "—"}</span>
    </div>
  );
}

// Engineering raw-data view: the last decoded datagram (auto-refreshing),
// plus resets, mode windows, and every raw state interval (unfiltered).
function Debug({ l, s, e }: P) {
  const { win } = useWindow();
  const now = useNow();
  // poll every 2s, but keep the previous data on screen during refetch so the
  // view doesn't flash "Loading" or lose scroll position
  const [data, setData] = useState<Awaited<ReturnType<typeof api.debug>>>();
  const [err, setErr] = useState<unknown>();
  useEffect(() => {
    setData(undefined);
    let live = true;
    const load = () => api.debug(l, s, e, win)
      .then((d) => { if (live) { setData(d); setErr(undefined); } })
      .catch((x) => { if (live) setErr(x); });
    load();
    const id = setInterval(load, 2000);
    return () => { live = false; clearInterval(id); };
  }, [l, s, e, win]);
  if (err && !data) return <ErrorBox err={err} />;
  if (!data) return <Loading />;
  const { live, resets, modes, states } = data;

  return (
    <>
      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <h2>Live telemetry</h2>
          <span className="muted" style={{ fontSize: 12 }}>refreshes every 2s</span>
        </div>
        {!live ? (
          <div className="empty">No datagram received yet for this EM.</div>
        ) : (
          <>
            <div className="kvgrid">
              <KV k="Seq" v={`${live.seq}`} mono />
              <KV k="Message" v={live.msg_type === 2 ? "heartbeat" : "event"} />
              <KV k="Active sequence" v={`${live.active_sequence}`} mono />
              <KV k="Step" v={`${live.step}${live.step_desc ? " · " + live.step_desc : ""}`} mono />
              <KV k="Step active" v={fmtMs(live.step_active_ms)} mono />
              <KV k="Last datagram" v={`${fmtSince(live.recv_time, now)} ago`} />
              <KV k="PLC clock skew"
                  v={live.plc_time ? `${live.skew_ms} ms` : "PLC clock unset"} mono />
            </div>

            <div className="label" style={{ marginTop: 16 }}>
              Status bits <span className="mono">0x{live.status_bits.toString(16).padStart(4, "0")}</span>
            </div>
            <div className="bitgrid">
              {live.status.map((b) => <BitChip key={b.name} {...b} kind="status" />)}
            </div>

            <div className="label" style={{ marginTop: 14 }}>
              Mode bits <span className="mono">0x{live.mode_bits.toString(16).padStart(4, "0")}</span>
            </div>
            <div className="bitgrid">
              {live.modes.map((b) => <BitChip key={b.name} {...b} kind="mode" />)}
            </div>

            <div className="label" style={{ marginTop: 16 }}>Text fields</div>
            <div className="kvgrid">
              <KV k="Alarm message" v={live.alarm_msg} mono />
              <KV k="Interlock fails" v={live.interlock_fails} mono />
              <KV k="Fault conditions" v={live.fault_conds} mono />
              <KV k="Waiting on" v={live.waiting_on} mono />
            </div>
          </>
        )}
      </div>

      <div className="card">
        <h2>Operator resets · {resets.length}</h2>
        {!resets.length ? (
          <div className="empty">No resets in this window.</div>
        ) : (
          <div className="tablewrap">
            <table className="data">
              <thead><tr><th>Time</th><th>Event</th></tr></thead>
              <tbody>
                {resets.map((rv, i) => (
                  <tr key={i}><td className="num">{fmtClock(rv.ts)}</td><td>{rv.event}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <h2>Mode windows · {modes.length}</h2>
        {!modes.length ? (
          <div className="empty">No mode changes in this window.</div>
        ) : (
          <div className="tablewrap">
            <table className="data">
              <thead><tr>
                <th>Flag</th><th>Start</th><th>End</th>
                <th style={{ textAlign: "right" }}>Duration</th>
              </tr></thead>
              <tbody>
                {modes.map((m, i) => (
                  <tr key={i}>
                    <td className="mono">{m.flag}</td>
                    <td className="num">{fmtClock(m.start_ts)}</td>
                    <td className="num">{fmtClock(m.end_ts)}</td>
                    <td className="num">{m.minutes.toFixed(1)} min</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <h2>Raw state intervals · {states.length}</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Every state the machine reported, before episode collapsing — fault,
          gate interlock, retry, and recovery each show as their own row.
        </p>
        {!states.length ? (
          <div className="empty">No state intervals in this window.</div>
        ) : (
          <div className="tablewrap">
            <table className="data">
              <thead><tr>
                <th>Start</th><th>State</th><th>Type</th><th>Reason</th><th>Step</th>
                <th style={{ textAlign: "right" }}>Duration</th>
              </tr></thead>
              <tbody>
                {states.map((st, i) => (
                  <tr key={i}>
                    <td className="num">{fmtClock(st.start_ts)}</td>
                    <td><StateChip state={st.state} /></td>
                    <td className="mono">{st.reason_type || ""}</td>
                    <td>{st.reason}</td>
                    <td className="num">{st.step_name}</td>
                    <td className="num">{st.seconds < 90 ? `${st.seconds.toFixed(1)} s` : `${(st.seconds / 60).toFixed(1)} min`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}

function T({ label, v }: { label: string; v: string }) {
  return (
    <div className="tile">
      <div className="n">{v}</div>
      <div className="label">{label}</div>
    </div>
  );
}
