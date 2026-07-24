import { createContext, useContext, useEffect, useState } from "react";
import { Interval, STATE_LABEL, STATE_ORDER, stateColor } from "../api";

// ── shared window (time range) context ───────────────────────────────────
const WindowCtx = createContext<{ win: string; setWin: (w: string) => void }>({
  win: "today", setWin: () => {},
});
export const WindowProvider = WindowCtx.Provider;
export const useWindow = () => useContext(WindowCtx);

const WINDOWS = ["1h", "8h", "today", "24h", "3d"];

export function WindowPicker() {
  const { win, setWin } = useWindow();
  return (
    <div className="winpick" role="group" aria-label="time window">
      {WINDOWS.map((w) => (
        <button key={w} className={w === win ? "active" : ""} onClick={() => setWin(w)}>
          {w}
        </button>
      ))}
    </div>
  );
}

// ── 1 Hz clock for dwell timers ──────────────────────────────────────────
export function useNow(): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

export function StateChip({ state }: { state: string }) {
  return (
    <span className="chip" style={{ background: stateColor(state) }}>
      {STATE_LABEL[state] ?? state}
    </span>
  );
}

// ── horizontal bar list ──────────────────────────────────────────────────
export function Bars({ rows, wrap, valueFmt }: {
  rows: { name: string; value: number; color?: string; suffix?: string }[];
  wrap?: boolean;
  valueFmt?: (v: number) => string;
}) {
  const max = Math.max(...rows.map((r) => r.value), 0.001);
  return (
    <div className={wrap ? "bars wrap" : "bars"}>
      {rows.map((r, i) => (
        <div className="row" key={i}>
          <span className="name" title={r.name}>{r.name}</span>
          <div className="track">
            <div className="fill" style={{
              width: `${(100 * r.value) / max}%`,
              background: r.color ?? "var(--grounded)",
            }} />
          </div>
          <span className="val">{valueFmt ? valueFmt(r.value) : `${r.value.toFixed(1)}${r.suffix ?? " min"}`}</span>
        </div>
      ))}
    </div>
  );
}

// axis tick label: time for short windows, date+time when spanning >~a day
function fmtTick(ms: number, spanMs: number): string {
  const d = new Date(ms);
  return spanMs > 26 * 3600 * 1000
    ? d.toLocaleString([], { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// ── SVG state gantt ──────────────────────────────────────────────────────
export function Gantt({ rows, from, to }: {
  rows: { label: string; intervals: Interval[] }[];
  from: number; to: number;
}) {
  const width = 1000, rowH = 26, labelW = 150;
  const span = Math.max(to - from, 1);
  const x = (t: number) => labelW + ((t - from) / span) * (width - labelW);
  const height = rows.length * rowH + 22;
  const usedStates = new Set<string>();
  rows.forEach((r) => r.intervals.forEach((iv) => usedStates.add(iv.state)));
  return (
    <div>
      <svg className="gantt" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none"
           style={{ height: Math.min(420, height) }}>
        <defs>
          {/* diagonal hatch = "no data / not reporting", so gaps read as
              nothing rather than a flat grey state like waiting */}
          <pattern id="gantt-nodata" width="7" height="7" patternUnits="userSpaceOnUse"
                   patternTransform="rotate(45)">
            <rect width="7" height="7" fill="var(--st-no_data)" />
            <line x1="0" y1="0" x2="0" y2="7" stroke="var(--subtle)" strokeWidth="2.5" />
          </pattern>
        </defs>
        {rows.map((r, i) => (
          <g key={r.label}>
            <text className="rowlabel" x={0} y={i * rowH + 17}>{r.label}</text>
            <rect x={labelW} y={i * rowH + 4} width={width - labelW} height={rowH - 8}
                  fill="url(#gantt-nodata)" rx={3} />
            {r.intervals.map((iv, j) => {
              const s = Math.max(Date.parse(iv.start_ts), from);
              const e = Math.min(Date.parse(iv.end_ts), to);
              if (e <= s) return null;
              return (
                <rect key={j} x={x(s)} y={i * rowH + 4}
                      width={Math.max(x(e) - x(s), 1.5)} height={rowH - 8} rx={2}
                      fill={stateColor(iv.state)}>
                  <title>{`${STATE_LABEL[iv.state] ?? iv.state}  ${new Date(s).toLocaleTimeString()} – ${new Date(e).toLocaleTimeString()}${iv.reason ? "\n" + iv.reason : ""}`}</title>
                </rect>
              );
            })}
          </g>
        ))}
      </svg>
      {/* time axis as HTML (the SVG is non-uniformly scaled, so text there
          would stretch); aligned under the plot area, which starts at labelW */}
      <div className="gantt-axis" style={{ marginLeft: `${(labelW / width) * 100}%` }}>
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <span key={f}>{fmtTick(from + span * f, span)}</span>
        ))}
      </div>
      <div className="gantt-legend">
        {STATE_ORDER.filter((s) => usedStates.has(s)).map((s) => (
          <span key={s}><i style={{ background: stateColor(s) }} />{STATE_LABEL[s]}</span>
        ))}
        <span><i className="nodata" />No data (not reporting)</span>
      </div>
    </div>
  );
}

// ── simple SVG trend (cycle times) ───────────────────────────────────────
export function Trend({ points, unit }: { points: { t: number; v: number }[]; unit: string }) {
  if (points.length < 2) return <div className="empty">Not enough data for a trend.</div>;
  const width = 1000, height = 180, pad = 40;
  const ts = points.map((p) => p.t), vs = points.map((p) => p.v);
  const t0 = Math.min(...ts), t1 = Math.max(...ts);
  const vmax = Math.max(...vs) * 1.1;
  const x = (t: number) => pad + ((t - t0) / Math.max(t1 - t0, 1)) * (width - pad - 8);
  const y = (v: number) => height - 24 - (v / vmax) * (height - 40);
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", height: 180 }}>
      {[0.25, 0.5, 0.75, 1].map((f) => (
        <g key={f}>
          <line x1={pad} x2={width - 8} y1={y(vmax * f / 1.1)} y2={y(vmax * f / 1.1)}
                stroke="var(--conduit)" strokeWidth={1} />
          <text x={pad - 6} y={y(vmax * f / 1.1) + 4} textAnchor="end"
                fontSize={10} fill="var(--secondary)">
            {Math.round(vmax * f / 1.1 / 1000)}{unit}
          </text>
        </g>
      ))}
      <path d={d} fill="none" stroke="var(--grounded)" strokeWidth={1.6} />
      {points.map((p, i) => (
        <circle key={i} cx={x(p.t)} cy={y(p.v)} r={2} fill="var(--grounded)" />
      ))}
    </svg>
  );
}

// ── vertical bar chart over time (throughput) ────────────────────────────
export function VBars({ bars, unit }: { bars: { t: number; v: number; label: string }[]; unit: string }) {
  if (!bars.length) return <div className="empty">No data in this window.</div>;
  const width = 1000, height = 190, padL = 40, padB = 24, padT = 8;
  const vmax = Math.max(...bars.map((b) => b.v), 1) * 1.1;
  const plotW = width - padL - 8, plotH = height - padB - padT;
  const bw = plotW / bars.length;
  const y = (v: number) => padT + plotH - (v / vmax) * plotH;
  const labelEvery = Math.ceil(bars.length / 8);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", height }}>
      {[0.25, 0.5, 0.75, 1].map((f) => (
        <g key={f}>
          <line x1={padL} x2={width - 8} y1={y(vmax * f / 1.1)} y2={y(vmax * f / 1.1)}
                stroke="var(--conduit)" strokeWidth={1} />
          <text x={padL - 6} y={y(vmax * f / 1.1) + 4} textAnchor="end"
                fontSize={10} fill="var(--secondary)">{Math.round(vmax * f / 1.1)}{unit}</text>
        </g>
      ))}
      {bars.map((b, i) => {
        const x = padL + i * bw;
        const h = padT + plotH - y(b.v);
        return (
          <g key={i}>
            <rect x={x + bw * 0.12} y={y(b.v)} width={bw * 0.76} height={Math.max(h, 0)}
                  rx={2} fill="var(--grounded)">
              <title>{`${b.label}\n${b.v}${unit ? " " + unit : ""}`}</title>
            </rect>
            {i % labelEvery === 0 && (
              <text x={x + bw / 2} y={height - 8} textAnchor="middle"
                    fontSize={10} fill="var(--secondary)">{b.label}</text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

export function Loading() {
  return <div className="empty">Loading…</div>;
}

export function ErrorBox({ err }: { err: unknown }) {
  return <div className="empty">Could not load: {String(err)}</div>;
}

// tiny fetch hook
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): { data?: T; err?: unknown } {
  const [state, setState] = useState<{ data?: T; err?: unknown }>({});
  useEffect(() => {
    let live = true;
    setState({});
    fn().then((data) => live && setState({ data })).catch((err) => live && setState({ err }));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return state;
}

// default background-refresh cadence for historical/aggregate views
export const REFRESH_MS = 15000;

// like useAsync but re-fetches on an interval, KEEPING the current data during
// each refetch — no loading flash, no scroll jump. Clears (shows Loading) only
// when deps change; ignores transient poll errors once data is present.
export function usePolledAsync<T>(fn: () => Promise<T>, deps: unknown[],
  intervalMs = REFRESH_MS): { data?: T; err?: unknown } {
  const [state, setState] = useState<{ data?: T; err?: unknown }>({});
  useEffect(() => {
    let live = true;
    setState({});
    const load = () => fn()
      .then((data) => { if (live) setState({ data }); })
      .catch((err) => { if (live) setState((s) => (s.data ? s : { err })); });
    load();
    const id = setInterval(load, intervalMs);
    return () => { live = false; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, intervalMs]);
  return state;
}
