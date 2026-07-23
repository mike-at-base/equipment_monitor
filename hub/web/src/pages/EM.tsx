import { NavLink, Route, Routes, useParams } from "react-router-dom";
import { api, fmtClock, fmtMs, stateColor, STATE_LABEL, STATE_ORDER } from "../api";
import { Bars, ErrorBox, Gantt, Loading, Trend, useAsync, useWindow } from "../components/ui";

// EM drill-down: Steps / Cycles / Availability / Alarms
export default function EMPage() {
  const { line = "", station = "", label = "" } = useParams();
  const tabs = [
    { path: "steps", name: "Step history" },
    { path: "cycles", name: "Cycle time" },
    { path: "availability", name: "Availability" },
    { path: "alarms", name: "Alarm history" },
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
  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;
  const { stats, cycles } = q.data;
  const points = [...cycles].reverse().map((c) => ({ t: Date.parse(c.start_ts), v: c.total_ms }));
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
                <tr key={i}>
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
  const avail = STATE_ORDER.filter((st) => !["down", "manual", "offline", "no_data"].includes(st))
    .reduce((a, st) => a + (spanMs[st] ?? 0), 0);
  const down = spanMs["down"] ?? 0;
  const pct = avail + down > 0 ? (100 * avail) / (avail + down) : null;
  const mttr = downs.data.downs;
  const acked = mttr.filter((d) => d.response_min != null);

  return (
    <>
      <div className="tiles" style={{ marginTop: 16 }}>
        <T label={`Availability (${win})`} v={pct != null ? `${pct.toFixed(1)}%` : "–"} />
        <T label="Down events" v={`${mttr.length}`} />
        <T label="Down minutes" v={(down / 60000).toFixed(1)} />
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
  const { downs } = q.data;
  if (!downs.length) return <div className="empty">No down events in this window. 🎉</div>;
  return (
    <div className="card">
      <h2>Down / alarm history · {downs.length} events</h2>
      <div className="tablewrap">
        <table className="data">
          <thead><tr>
            <th>Start</th><th>Type</th><th>Reason</th><th>Step</th>
            <th style={{ textAlign: "right" }}>Duration</th>
            <th style={{ textAlign: "right" }}>Response</th>
            <th style={{ textAlign: "right" }}>Repair</th>
          </tr></thead>
          <tbody>
            {downs.map((d, i) => (
              <tr key={i}>
                <td className="num">{fmtClock(d.start_ts)}</td>
                <td>{d.reason_type}</td>
                <td>{d.reason}</td>
                <td className="num">{d.step_name}</td>
                <td className="num">{d.minutes.toFixed(1)} min</td>
                <td className="num">{d.response_min != null ? `${d.response_min.toFixed(1)} min` : "–"}</td>
                <td className="num">{d.repair_min != null ? `${d.repair_min.toFixed(1)} min` : "–"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
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
