import { useEffect, useRef, useState } from "react";
import { NavLink, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { api, CycleRow, fmtClock, fmtMs, fmtSince, SeqConfig, stateColor, StepStat, STATE_LABEL, STATE_ORDER, winLabel } from "../api";
import { Bars, BoxPlot, ErrorBox, Gantt, Histogram, Loading, PercentileBand, StackedBars, StateChip, Trend, useAsync, useNow, usePolledAsync, useWindow, VBars } from "../components/ui";

// EM drill-down: Steps / Cycles / Availability / Alarms
export default function EMPage() {
  const { line = "", station = "", label = "" } = useParams();
  const tabs = [
    { path: "steps", name: "Step history" },
    { path: "cycles", name: "Cycle time" },
    { path: "availability", name: "Availability" },
    { path: "alarms", name: "Alarm history" },
    { path: "debug", name: "Raw / debug" },
    { path: "config", name: "Config" },
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
        <Route path="config" element={<Config l={line} s={station} e={label} />} />
        <Route path="*" element={<Steps l={line} s={station} e={label} />} />
      </Routes>
    </>
  );
}

type P = { l: string; s: string; e: string };

const STEPS_PAGE = 800;

function flowBucketLabel(iso: string, bucket: string): string {
  const d = new Date(iso);
  if (bucket === "1d") {
    return d.toLocaleDateString([], { month: "numeric", day: "numeric" });
  }
  if (bucket === "4h") {
    return d.toLocaleString([], { month: "numeric", day: "numeric", hour: "2-digit" });
  }
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function flowSeriesColor(state: string, i: number): string {
  if (state === "other") return "var(--st-wait)";
  if (state === "starved") {
    const shades = ["var(--st-starved)", "#e0a82e", "#c99620", "#f5d56a"];
    return shades[i % shades.length];
  }
  if (state === "blocked") {
    const shades = ["var(--st-blocked)", "#d45a20", "#f09064", "#b84818"];
    return shades[i % shades.length];
  }
  const palette = ["#048ee5", "#77a45a", "#9b59b6", "#54524f"];
  return palette[i % palette.length];
}

function Steps({ l, s, e }: P) {
  const { win } = useWindow();
  const [page, setPage] = useState(0); // 0-based; newest-first pages
  useEffect(() => { setPage(0); }, [l, s, e, win]);
  const q = usePolledAsync(
    () => api.steps(l, s, e, win, STEPS_PAGE, page * STEPS_PAGE),
    [l, s, e, win, page],
  );
  // Clamp if total shrank (e.g. window rolled) while sitting on a deep page.
  useEffect(() => {
    if (!q.data) return;
    const pages = Math.max(1, Math.ceil(q.data.total / STEPS_PAGE));
    if (page >= pages) setPage(pages - 1);
  }, [q.data, page]);
  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;
  const { steps, total } = q.data;
  const pageCount = Math.max(1, Math.ceil(total / STEPS_PAGE));
  const fromN = total === 0 ? 0 : page * STEPS_PAGE + 1;
  const toN = Math.min((page + 1) * STEPS_PAGE, total);

  return (
    <>
      <StepSpread l={l} s={s} e={e} />
      <div className="card">
        <div className="pager">
          <h2 style={{ margin: 0 }}>
            Step history · {fromN}–{toN} of {total}
          </h2>
          <div className="pager-controls">
            <button type="button" disabled={page <= 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}>Newer</button>
            <span className="muted">page {Math.min(page + 1, pageCount)} / {pageCount}</span>
            <button type="button" disabled={page + 1 >= pageCount}
              onClick={() => setPage((p) => p + 1)}>Older</button>
          </div>
        </div>
        <div className="tablewrap">
          <table className="data">
            <thead><tr>
              <th>Time</th><th>Seq</th><th>Step</th><th>Description</th>
              <th style={{ textAlign: "right" }}>Duration</th>
              <th>Branch →</th><th>Faulted</th>
            </tr></thead>
            <tbody>
              {steps.map((st, i) => (
                <tr key={`${st.start_ts}-${st.step}-${i}`}>
                  <td className="num">{fmtClock(st.start_ts)}</td>
                  <td className="num">{st.seq_index}</td>
                  <td className="num">{st.step}</td>
                  <td>{st.description}</td>
                  <td className="num">{fmtMs(st.duration_ms)}</td>
                  <td className="num" title={st.branch_taken
                    ? `sequencer took the branch to step ${st.branch_taken}`
                    : "no branch resolved (pre-v5 PLC, or forced jump / fault / reset)"}>
                    {st.branch_taken || "—"}
                  </td>
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

// Duration spread per step, over the WHOLE window (the table below is
// paged; averaging only the visible page is a biased sample). Pick a step
// to see its numbers.
function StepSpread({ l, s, e }: P) {
  const { win } = useWindow();
  const q = usePolledAsync(() => api.stepStats(l, s, e, win), [l, s, e, win]);
  const [sel, setSel] = useState<string>();
  const [showAll, setShowAll] = useState(false);
  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;
  const all = q.data.steps;
  if (all.length === 0) {
    return (
      <div className="card">
        <h2>Step duration spread ({winLabel(win)})</h2>
        <div className="empty">No step history in this window.</div>
      </div>
    );
  }
  const multiSeq = new Set(all.map((r) => r.seq_index)).size > 1;
  const key = (r: StepStat) => (multiSeq ? `${r.seq_index}·${r.step}` : r.step);
  const shown = showAll ? all : all.slice(0, 15);
  const picked = all.find((r) => key(r) === sel);

  return (
    <>
      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
          <h2 style={{ margin: 0 }}>Step duration spread ({winLabel(win)})</h2>
          <span className="muted" style={{ fontSize: 13 }}>
            {all.length} steps · slowest median first · click a step for detail
          </span>
        </div>
        <BoxPlot
          rows={shown.map((r) => ({
            name: key(r),
            detail: r.description,
            min: r.min_ms, p05: r.p05_ms, p25: r.p25_ms, p50: r.p50_ms,
            p75: r.p75_ms, p95: r.p95_ms, max: r.max_ms, n: r.count,
            flagged: r.faulted > 0,
          }))}
          fmt={(v) => fmtMs(v)}
          selected={sel}
          onSelect={(n) => setSel(n === sel ? undefined : n)}
        />
        {all.length > 15 && (
          <button type="button" className="linkbtn" style={{ marginTop: 10 }}
                  onClick={() => setShowAll((v) => !v)}>
            {showAll ? "Show top 15" : `Show all ${all.length} steps`}
          </button>
        )}
      </div>
      {picked && (
        <div className="card">
          <h2>
            Step {picked.step}
            {picked.description ? ` · ${picked.description}` : ""}
          </h2>
          <div className="tiles">
            <T label="Executions" v={`${picked.count}`} />
            <T label="Faulted" v={`${picked.faulted}`} />
            <T label="Min" v={fmtMs(picked.min_ms)} />
            <T label="p25" v={fmtMs(picked.p25_ms)} />
            <T label="Median" v={fmtMs(picked.p50_ms)} />
            <T label="p75" v={fmtMs(picked.p75_ms)} />
            <T label="p95" v={fmtMs(picked.p95_ms)} />
            <T label="Max" v={fmtMs(picked.max_ms)} />
          </div>
          <p className="muted" style={{ marginTop: 12, marginBottom: 0 }}>
            Spread p25–p75 is {fmtMs(picked.p75_ms - picked.p25_ms)}; the
            slowest execution took {fmtMs(picked.max_ms)}, {" "}
            {picked.p50_ms > 0 ? (picked.max_ms / picked.p50_ms).toFixed(1) : "–"}× the median.
          </p>
        </div>
      )}
      {picked && <StepShapeAndDrift l={l} s={s} e={e} step={picked.step}
                                    seq={picked.seq_index} />}
    </>
  );
}

// The two questions the box plot raises but cannot answer: what SHAPE is
// the distribution (one tight mode, or two?), and is the spread MOVING?
function StepShapeAndDrift({ l, s, e, step, seq }: P & { step: string; seq: number }) {
  const { win } = useWindow();
  const q = usePolledAsync(() => api.stepDetail(l, s, e, win, step, seq),
    [l, s, e, win, step, seq]);
  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;
  const { histogram: h, drift, bucket } = q.data;
  const total = h.bins.reduce((a, b) => a + b, 0) + h.overflow;
  return (
    <>
      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
          <h2 style={{ margin: 0 }}>Distribution · step {step}</h2>
          <span className="muted" style={{ fontSize: 13 }}>
            {total} executions · bins to p95
            {h.overflow > 0 && ` · ${h.overflow} slower than ${fmtMs(h.hi_ms)}`}
          </span>
        </div>
        <Histogram bins={h.bins} lo={h.lo_ms} hi={h.hi_ms} overflow={h.overflow}
                   fmt={(v) => fmtMs(v)} />
      </div>
      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
          <h2 style={{ margin: 0 }}>Drift over time · step {step}</h2>
          <span className="muted" style={{ fontSize: 13 }}>{bucket} buckets</span>
        </div>
        <PercentileBand
          points={drift.map((d) => ({
            t: Date.parse(d.bucket_ts), n: d.count,
            p25: d.p25_ms, p50: d.p50_ms, p75: d.p75_ms, p95: d.p95_ms,
          }))}
          fmt={(v) => fmtMs(v)} />
      </div>
    </>
  );
}

function Cycles({ l, s, e }: P) {
  const { win } = useWindow();
  const q = usePolledAsync(() => api.cycles(l, s, e, win), [l, s, e, win]);
  const [selTs, setSelTs] = useState<string>();
  const [pct, setPct] = useState<string>();
  const waterfallRef = useRef<HTMLDivElement>(null);
  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;
  const { stats, cycles } = q.data;
  const points = [...cycles].reverse().map((c) => ({ t: Date.parse(c.start_ts), v: c.total_ms }));
  const selected = cycles.find((c) => c.start_ts === selTs) ?? cycles[0];

  // Percentiles come from percentile_cont, which INTERPOLATES — the p50
  // value often belongs to no actual cycle. And the stats span the whole
  // window while this list is capped. So jump to the cycle nearest the
  // value: always a real cycle, and never contradicts the tile.
  const pickPct = (name: string, ms?: number) => {
    if (ms == null || cycles.length === 0) return;
    let best = cycles[0];
    for (const c of cycles) {
      if (Math.abs(c.total_ms - ms) < Math.abs(best.total_ms - ms)) best = c;
    }
    setSelTs(best.start_ts);
    setPct(name);
    requestAnimationFrame(() =>
      waterfallRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };
  const pctTile = (name: string, ms?: number) => ({
    onClick: ms != null && cycles.length > 0 ? () => pickPct(name, ms) : undefined,
    active: pct === name,
    title: ms != null ? `Show the cycle nearest ${name} (${fmtMs(ms)})` : undefined,
  });

  return (
    <>
      <div className="tiles" style={{ marginTop: 16 }}>
        <T label="Cycles" v={`${stats.count}`} />
        <T label="Per hour" v={stats.per_hour != null ? `${stats.per_hour}` : "–"} />
        <T label="p10" v={fmtMs(stats.p10_ms)} {...pctTile("p10", stats.p10_ms)} />
        <T label="p50" v={fmtMs(stats.p50_ms)} {...pctTile("p50", stats.p50_ms)} />
        <T label="p90" v={fmtMs(stats.p90_ms)} {...pctTile("p90", stats.p90_ms)} />
        <T label="Work avg" v={fmtMs(stats.work_avg_ms)} />
        <T label="Exchange avg" v={fmtMs(stats.exchange_avg_ms)} />
        <T label="Interrupted" v={`${stats.interrupted}`} />
      </div>
      <div className="card">
        <h2>Cycle time trend ({winLabel(win)})</h2>
        <Trend points={points} unit="s" />
      </div>
      <Throughput l={l} s={s} e={e} />
      {selected && (
        <div className="card" ref={waterfallRef}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
            <h2 style={{ margin: 0 }}>
              Step waterfall · cycle at {fmtClock(selected.start_ts)}
              {pct && <span className="muted" style={{ fontWeight: 400 }}> · nearest {pct}</span>}
            </h2>
            <span className="muted" style={{ fontSize: 13 }}>
              {fmtMs(selected.total_ms)} total
              {selected.interrupted && <> · <span style={{ color: "var(--st-down)" }}>interrupted</span></>}
              {" · click a p10/p50/p90 tile or a row below"}
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
                <tr key={i} onClick={() => { setSelTs(c.start_ts); setPct(undefined); }}
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
  const q = usePolledAsync(() => api.throughput(l, s, e, win, bucket), [l, s, e, win, bucket]);
  const bars = (q.data?.buckets ?? []).map((b) => ({
    t: Date.parse(b.bucket_ts), v: b.count,
    label: fmtClock(b.bucket_ts).replace(/:\d\d /, " "),
  }));
  return (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
        <h2 style={{ margin: 0 }}>Throughput ({winLabel(win)})</h2>
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
  const steps = q.data.steps
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
  const iv = usePolledAsync(() => api.intervals(l, s, e, win), [l, s, e, win]);
  const downs = usePolledAsync(() => api.downs(l, s, e, win), [l, s, e, win]);
  if (iv.err) return <ErrorBox err={iv.err} />;
  if (!iv.data || !downs.data) return <Loading />;

  // window bounds come from the API (the same [from,to] the numbers use), so
  // the timeline spans the exact requested window, not just the intervals.
  const from = Date.parse(downs.data.from);
  const to = Date.parse(downs.data.to);
  const windowMin = (to - from) / 60000;

  // time-by-state is the API's window-clipped state_min (includes the live
  // open interval). Any remainder is time the EM wasn't reporting → "no data".
  const stateMin = downs.data.state_min;
  const coveredMin = Object.values(stateMin).reduce((a, b) => a + b, 0);
  const noDataMin = Math.max(0, windowMin - coveredMin);

  const eps = downs.data.episodes;
  const epMin = eps.reduce((a, e) => a + e.minutes, 0);
  const acked = eps.filter((e) => e.response_min != null);
  const flowTimeline = downs.data.flow_reasons_timeline;

  return (
    <>
      <div className="tiles" style={{ marginTop: 16 }}>
        <T label={`Availability (${winLabel(win)})`}
           v={downs.data.availability_pct != null ? `${downs.data.availability_pct.toFixed(1)}%` : "–"} />
        <T label="Down episodes" v={`${eps.length}`} />
        <T label="Down minutes" v={epMin.toFixed(1)} />
        <T label="Retries" v={`${eps.reduce((a, e) => a + e.retries, 0)}`} />
        <T label="Avg response" v={acked.length ? `${(acked.reduce((a, d) => a + d.response_min!, 0) / acked.length).toFixed(1)} min` : "–"} />
        <T label="Avg repair" v={acked.length ? `${(acked.reduce((a, d) => a + d.repair_min!, 0) / acked.length).toFixed(1)} min` : "–"} />
      </div>
      <div className="card">
        <h2>State timeline</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          {fmtClock(downs.data.from)} → {fmtClock(downs.data.to)}
        </p>
        <Gantt rows={[{ label: s, intervals: iv.data }]} from={from} to={to} />
      </div>
      <div className="card">
        <h2>Time by state <span className="muted" style={{ fontSize: 13, fontWeight: 400 }}>
          ({coveredMin.toFixed(1)} of {windowMin.toFixed(0)} min reported)</span></h2>
        <Bars rows={[
          ...STATE_ORDER.filter((st) => stateMin[st])
            .map((st) => ({ name: STATE_LABEL[st], value: stateMin[st], color: stateColor(st) })),
          ...(noDataMin > 0.1 ? [{ name: "No data (not reporting)", value: noDataMin, color: "var(--st-no_data)" }] : []),
        ]} />
      </div>
      {(downs.data.flow_reasons ?? []).length > 0 && (
        <div className="card">
          <h2>Starved / blocked reasons</h2>
          <p className="muted" style={{ marginTop: 0 }}>
            Why the EM was waiting on flow — minutes charged to each waiting-on
            reason (from the PLC permissives).
          </p>
          <Bars wrap rows={(downs.data.flow_reasons ?? []).map((r) => ({
            name: `${STATE_LABEL[r.state] ?? r.state} — ${r.reason} (×${r.count})`,
            value: r.minutes,
            color: stateColor(r.state),
          }))} />
        </div>
      )}
      {(flowTimeline?.series?.length ?? 0) > 0 && flowTimeline && (
        <div className="card">
          <h2>Flow reasons over time
            <span className="muted" style={{ fontSize: 13, fontWeight: 400 }}>
              {" "}· {flowTimeline.bucket} buckets
            </span>
          </h2>
          <p className="muted" style={{ marginTop: 0 }}>
            Minutes of starved/blocked time by waiting-on reason in each bucket —
            shows how the constraint shifts across the window (top reasons; rest as Other).
          </p>
          <StackedBars
            labels={flowTimeline.buckets.map((b) => flowBucketLabel(b, flowTimeline.bucket))}
            series={flowTimeline.series.map((s, i) => {
              const short = s.reason.length > 42 ? `${s.reason.slice(0, 40)}…` : s.reason;
              return {
                name: s.reason === "Other" ? "Other"
                  : `${STATE_LABEL[s.state] ?? s.state}: ${short}`,
                color: flowSeriesColor(s.state, i),
                values: s.minutes,
              };
            })}
          />
        </div>
      )}
      {downs.data.top_reasons.length > 0 && (
        <div className="card">
          <h2>Down reasons</h2>
          <Bars wrap rows={downs.data.top_reasons.map((r) => ({
            name: `${r.reason} (×${r.count})`, value: r.minutes, color: "var(--st-down)",
          }))} />
        </div>
      )}
    </>
  );
}

function Alarms({ l, s, e }: P) {
  const { win } = useWindow();
  const q = usePolledAsync(() => api.downs(l, s, e, win), [l, s, e, win]);
  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;
  const { episodes } = q.data;
  if (!episodes.length) return <div className="empty">No downtime episodes in this window. 🎉</div>;

  // aggregate episodes by root-cause reason → occurrences + total downtime
  const byReason = new Map<string, { count: number; minutes: number }>();
  for (const ep of episodes) {
    const k = ep.reason || ep.reason_type || "Unknown";
    const a = byReason.get(k) ?? { count: 0, minutes: 0 };
    a.count += 1; a.minutes += ep.minutes;
    byReason.set(k, a);
  }
  const reasons = [...byReason.entries()].map(([reason, a]) => ({ reason, ...a }));
  const byCount = [...reasons].sort((a, b) => b.count - a.count).slice(0, 10);
  const byDuration = [...reasons].sort((a, b) => b.minutes - a.minutes).slice(0, 10);
  // mean time to recover per root cause = total downtime / occurrences
  const byMttr = [...reasons].map((r) => ({ ...r, mttr: r.minutes / r.count }))
    .sort((a, b) => b.mttr - a.mttr).slice(0, 10);

  // faults localized to the step they occurred on (step-fault episodes)
  const byStep = new Map<string, number>();
  const byStepMin = new Map<string, number>();
  for (const ep of episodes) if (ep.step_name) {
    byStep.set(ep.step_name, (byStep.get(ep.step_name) ?? 0) + 1);
    byStepMin.set(ep.step_name, (byStepMin.get(ep.step_name) ?? 0) + ep.minutes);
  }
  const stepBars = [...byStep.entries()]
    .map(([step, count]) => ({ name: `Step ${step}`, value: count, color: "var(--st-down)" }))
    .sort((a, b) => b.value - a.value).slice(0, 12);
  const stepMinBars = [...byStepMin.entries()]
    .map(([step, minutes]) => ({ name: `Step ${step}`, value: minutes, color: "var(--st-down)" }))
    .sort((a, b) => b.value - a.value).slice(0, 12);

  // alarm occurrences over time (hourly buckets by episode start)
  const hourMap = new Map<number, number>();
  for (const ep of episodes) {
    const d = new Date(ep.start_ts); d.setMinutes(0, 0, 0);
    hourMap.set(d.getTime(), (hourMap.get(d.getTime()) ?? 0) + 1);
  }
  const overTime = [...hourMap.entries()].sort((a, b) => a[0] - b[0])
    .map(([t, c]) => ({ t, v: c, label: fmtClock(new Date(t).toISOString()).replace(/:\d\d /, " ") }));

  const intFmt = (v: number) => `${v}`;
  return (
    <>
      <div className="card">
        <h2>Alarms by count</h2>
        <p className="muted" style={{ marginTop: 0 }}>How often each root cause tripped — nuisance / chronic alarms.</p>
        <Bars wrap valueFmt={intFmt} rows={byCount.map((r) => ({
          name: r.reason, value: r.count, color: "var(--st-down)",
        }))} />
      </div>
      <div className="card">
        <h2>Alarms by downtime</h2>
        <p className="muted" style={{ marginTop: 0 }}>Total minutes lost to each root cause — biggest availability hits.</p>
        <Bars wrap rows={byDuration.map((r) => ({
          name: `${r.reason} (×${r.count})`, value: r.minutes, color: "var(--st-down)",
        }))} />
      </div>
      <div className="card">
        <h2>Mean time to recover</h2>
        <p className="muted" style={{ marginTop: 0 }}>Average downtime per occurrence — which alarms are hard to clear and need a recovery procedure.</p>
        <Bars wrap rows={byMttr.map((r) => ({
          name: `${r.reason} (×${r.count})`, value: r.mttr, color: "var(--st-down)",
        }))} />
      </div>
      {stepBars.length > 0 && (
        <div className="card">
          <h2>Faults by step</h2>
          <p className="muted" style={{ marginTop: 0 }}>Where in the sequence faults land — points at the failing motion or device.</p>
          <Bars valueFmt={intFmt} rows={stepBars} />
        </div>
      )}
      {stepMinBars.length > 0 && (
        <div className="card">
          <h2>Downtime by step</h2>
          <p className="muted" style={{ marginTop: 0 }}>Minutes lost at each step — the duration view of the failing motion or device.</p>
          <Bars rows={stepMinBars} />
        </div>
      )}
      {overTime.length > 1 && (
        <div className="card">
          <h2>Alarms over time</h2>
          <p className="muted" style={{ marginTop: 0 }}>Occurrences per hour — reveals clustering at shift change, ramp-up, or after breaks.</p>
          <VBars bars={overTime} unit="" />
        </div>
      )}
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
    </>
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
              <KV k="Waiting on (union of all branches)" v={live.waiting_on} mono />
              <KV k="Branch taken (v5)"
                  v={live.branch_taken || (live.wire_version >= 5 ? "— unresolved" : "— PLC is v" + live.wire_version)} mono />
              <KV k="Dwell reason (v5, attributed)"
                  v={live.dwell_reason || (live.wire_version >= 5 ? "— unresolved" : "n/a")} mono />
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

// Review & confirm: name the EM, set per-sequence cycle metadata (cycle
// start/complete chosen from steps actually observed), and confirm it. Saving
// persists to the DB and live-reloads the tracker.
function Config({ l, s, e }: P) {
  const q = useAsync(() => api.emConfig(l, s, e), [l, s, e]);
  const [displayName, setDisplayName] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [seqs, setSeqs] = useState<SeqConfig[]>([]);
  const [observed, setObserved] = useState<Record<number, string[]>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveErr, setSaveErr] = useState<unknown>();
  const nav = useNavigate();

  useEffect(() => {
    if (!q.data) return;
    const c = q.data;
    setDisplayName(c.display_name);
    setConfirmed(c.confirmed);
    const obs: Record<number, string[]> = {};
    for (const o of c.observed) obs[o.seq_index] = o.steps;
    setObserved(obs);
    const idxs = [...new Set([...c.sequences.map((x) => x.index), ...c.observed.map((o) => o.seq_index)])]
      .sort((a, b) => a - b);
    setSeqs(idxs.map((idx) => c.sequences.find((x) => x.index === idx) ?? {
      index: idx, name: "", is_production: false, cycle_start_step: "", cycle_complete_step: "",
      starved_steps: [], blocked_steps: [], nva_steps: [],
    }));
  }, [q.data]);

  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;
  const c = q.data;

  const setSeq = (i: number, patch: Partial<SeqConfig>) =>
    setSeqs((cur) => cur.map((sq, j) => (j === i ? { ...sq, ...patch } : sq)));
  // every step relevant to a sequence (observed + already configured), so a
  // previously-tagged step still shows even if not observed this window
  const stepUniverse = (sq: SeqConfig) => {
    const set = new Set(observed[sq.index] ?? []);
    [sq.cycle_start_step, sq.cycle_complete_step].forEach((x) => x && set.add(x));
    sq.starved_steps.forEach((x) => set.add(x));
    sq.blocked_steps.forEach((x) => set.add(x));
    (sq.nva_steps ?? []).forEach((x) => set.add(x));
    return [...set].sort();
  };
  // toggle a step into starved/blocked/nva. The three are mutually
  // exclusive: a step is one kind of thing, so adding it to one list
  // removes it from the others; clicking its current tag clears it.
  type StepKind = "starved" | "blocked" | "nva";
  const toggleStep = (i: number, kind: StepKind, step: string) =>
    setSeqs((cur) => cur.map((sq, j) => {
      if (j !== i) return sq;
      const lists: Record<StepKind, string[]> = {
        starved: sq.starved_steps ?? [],
        blocked: sq.blocked_steps ?? [],
        nva: sq.nva_steps ?? [],
      };
      const had = lists[kind].includes(step);
      const next = {
        starved: lists.starved.filter((x) => x !== step),
        blocked: lists.blocked.filter((x) => x !== step),
        nva: lists.nva.filter((x) => x !== step),
      };
      if (!had) next[kind].push(step);
      return { ...sq, starved_steps: next.starved, blocked_steps: next.blocked,
               nva_steps: next.nva };
    }));
  const save = () => {
    setSaving(true); setSaved(false); setSaveErr(undefined);
    api.saveEMConfig(l, s, e, { display_name: displayName, confirmed, sequences: seqs })
      .then(() => setSaved(true)).catch(setSaveErr).finally(() => setSaving(false));
  };
  const del = () => {
    if (!window.confirm(`Delete ${c.station}${c.em_label !== "main" ? " · " + c.em_label : ""} and all its data? This can't be undone.`)) return;
    api.deleteEM(l, s, e).then(() => nav("/")).catch(setSaveErr);
  };

  return (
    <div className="card">
      <h2>Review &amp; confirm</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        {c.line} / {c.station}{c.em_label !== "main" ? ` · ${c.em_label}` : ""} · wire v{c.wire_version}
        {!c.confirmed && <span className="chip-unconfirmed" style={{ marginLeft: 8 }}>unconfirmed</span>}
      </p>

      <div className="cfg-field">
        <label>Display name</label>
        <input value={displayName} onChange={(ev) => setDisplayName(ev.target.value)} placeholder={c.em_label} />
      </div>

      <h3 className="cfg-sub">Sequences</h3>
      {seqs.length === 0 && <div className="muted">No sequences observed yet — run the machine, then reload.</div>}
      {seqs.map((sq, i) => (
        <div className="cfg-seq" key={sq.index}>
          <div className="cfg-seq-head">Sequence {sq.index}</div>
          <div className="cfg-grid">
            <div className="cfg-field">
              <label>Name</label>
              <input value={sq.name} onChange={(ev) => setSeq(i, { name: ev.target.value })} placeholder="e.g. Run" />
            </div>
            <div className="cfg-field">
              <label>Cycle start step</label>
              <input list={`steps-${sq.index}`} value={sq.cycle_start_step}
                     onChange={(ev) => setSeq(i, { cycle_start_step: ev.target.value })}
                     placeholder="type to search…" />
            </div>
            <div className="cfg-field">
              <label>Cycle complete step</label>
              <input list={`steps-${sq.index}`} value={sq.cycle_complete_step}
                     onChange={(ev) => setSeq(i, { cycle_complete_step: ev.target.value })}
                     placeholder="type to search…" />
            </div>
            <datalist id={`steps-${sq.index}`}>
              {stepUniverse(sq).map((x) => <option key={x} value={x} />)}
            </datalist>
            <label className="cfg-check">
              <input type="checkbox" checked={sq.is_production}
                     onChange={(ev) => setSeq(i, { is_production: ev.target.checked })} />
              Production sequence
            </label>
          </div>

          <div className="cfg-flow">
            <div className="cfg-flow-row">
              <span className="cfg-flow-label">Starved at <em>(waiting on upstream)</em></span>
              <StepPicker options={stepUniverse(sq)} selected={sq.starved_steps} cls="starved"
                onAdd={(x) => toggleStep(i, "starved", x)} onRemove={(x) => toggleStep(i, "starved", x)} />
            </div>
            <div className="cfg-flow-row">
              <span className="cfg-flow-label">Blocked at <em>(waiting on downstream)</em></span>
              <StepPicker options={stepUniverse(sq)} selected={sq.blocked_steps} cls="blocked"
                onAdd={(x) => toggleStep(i, "blocked", x)} onRemove={(x) => toggleStep(i, "blocked", x)} />
            </div>
            <div className="cfg-flow-row">
              <span className="cfg-flow-label">Non-value-added <em>(purge, prime, clean — running but not adding value)</em></span>
              <StepPicker options={stepUniverse(sq)} selected={sq.nva_steps ?? []} cls="nva"
                onAdd={(x) => toggleStep(i, "nva", x)} onRemove={(x) => toggleStep(i, "nva", x)} />
            </div>
          </div>
        </div>
      ))}

      <label className="cfg-check" style={{ marginTop: 16 }}>
        <input type="checkbox" checked={confirmed} onChange={(ev) => setConfirmed(ev.target.checked)} />
        Confirmed — identity and configuration verified
      </label>

      <div style={{ marginTop: 16, display: "flex", gap: 12, alignItems: "center" }}>
        <button className="btn-primary" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
        {saved && <span style={{ color: "var(--st-productive)" }}>Saved ✓</span>}
        {saveErr != null && <span style={{ color: "var(--st-down)" }}>Failed: {String(saveErr)}</span>}
        <span style={{ flex: 1 }} />
        <button className="btn-danger" onClick={del}>Delete EM</button>
      </div>
    </div>
  );
}

// Search-to-add step picker: shows only the selected steps as removable chips
// and adds via a type-to-filter box, so it scales to hundreds of long-named
// steps without a wall of chips.
function StepPicker({ options, selected, cls, onAdd, onRemove }: {
  options: string[]; selected: string[]; cls: "starved" | "blocked" | "nva";
  onAdd: (s: string) => void; onRemove: (s: string) => void;
}) {
  const [q, setQ] = useState("");
  const matches = q.trim()
    ? options.filter((o) => !selected.includes(o) && o.toLowerCase().includes(q.trim().toLowerCase())).slice(0, 10)
    : [];
  return (
    <div className="steppick">
      <div className="steppick-chips">
        {selected.length === 0 && <span className="muted" style={{ fontSize: 12 }}>none</span>}
        {selected.map((st) => (
          <span key={st} className={`stepchip on ${cls}`}>
            {st}
            <button type="button" className="chip-x" onClick={() => onRemove(st)} aria-label="remove">×</button>
          </span>
        ))}
      </div>
      <div className="steppick-search">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="type to add a step…" />
        {matches.length > 0 && (
          <div className="steppick-menu">
            {matches.map((m) => (
              <button type="button" key={m} className="steppick-opt"
                      onClick={() => { onAdd(m); setQ(""); }}>{m}</button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function T({ label, v, onClick, active, title }: {
  label: string; v: string;
  onClick?: () => void; active?: boolean; title?: string;
}) {
  if (!onClick) {
    return (
      <div className="tile">
        <div className="n">{v}</div>
        <div className="label">{label}</div>
      </div>
    );
  }
  // a real <button> so keyboard focus and Enter/Space work for free
  return (
    <button type="button" title={title} onClick={onClick}
            className={`tile tile-click${active ? " active" : ""}`}>
      <div className="n">{v}</div>
      <div className="label">{label}</div>
    </button>
  );
}
