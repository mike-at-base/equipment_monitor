import { createContext, useContext, useEffect, useRef, useState } from "react";
import { customWin, Interval, parseCustomWin, STATE_LABEL, STATE_ORDER, stateColor } from "../api";

// ── shared window (time range) context ───────────────────────────────────
const WindowCtx = createContext<{ win: string; setWin: (w: string) => void }>({
  win: "today", setWin: () => {},
});
export const WindowProvider = WindowCtx.Provider;
export const useWindow = () => useContext(WindowCtx);

const WINDOWS = ["1h", "8h", "today", "24h", "3d", "prod"];
const WIN_LABEL: Record<string, string> = { prod: "prod today" };

// <input type="datetime-local"> speaks LOCAL wall time with no zone, so
// convert through the local offset in both directions rather than slicing
// an ISO string (which would silently shift the range by the UTC offset).
function toLocalInput(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
    `T${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fromLocalInput(v: string): Date | null {
  const d = new Date(v); // parsed as local time
  return isNaN(d.getTime()) ? null : d;
}
function fmtRange(fromISO: string, toISO: string): string {
  const f = new Date(fromISO), t = new Date(toISO);
  const sameDay = f.toDateString() === t.toDateString();
  const hm = (d: Date) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const md = (d: Date) => d.toLocaleDateString([], { month: "numeric", day: "numeric" });
  return sameDay
    ? `${md(f)} ${hm(f)}–${hm(t)}`
    : `${md(f)} ${hm(f)} – ${md(t)} ${hm(t)}`;
}

export function WindowPicker() {
  const { win, setWin } = useWindow();
  const [open, setOpen] = useState(false);
  const custom = parseCustomWin(win);
  const [from, setFrom] = useState(() => toLocalInput(new Date(Date.now() - 8 * 3600e3)));
  const [to, setTo] = useState(() => toLocalInput(new Date()));

  // seed the fields from the active range when opening
  const openPanel = () => {
    if (custom) {
      setFrom(toLocalInput(new Date(custom.from)));
      setTo(toLocalInput(new Date(custom.to)));
    } else {
      setFrom(toLocalInput(new Date(Date.now() - 8 * 3600e3)));
      setTo(toLocalInput(new Date()));
    }
    setOpen((v) => !v);
  };

  const f = fromLocalInput(from), t = fromLocalInput(to);
  const invalid = !f || !t || t <= f;
  const apply = () => {
    if (invalid) return;
    setWin(customWin(f!.toISOString(), t!.toISOString()));
    setOpen(false);
  };

  return (
    <div className="winpick-wrap">
      <div className="winpick" role="group" aria-label="time window">
        {WINDOWS.map((w) => (
          <button key={w} className={w === win ? "active" : ""} onClick={() => setWin(w)}>
            {WIN_LABEL[w] ?? w}
          </button>
        ))}
        <button className={custom ? "active" : ""} onClick={openPanel}
                title="Pick an absolute date/time range">
          {custom ? fmtRange(custom.from, custom.to) : "custom"}
        </button>
      </div>
      {open && (
        <>
          <div className="winpick-scrim" onClick={() => setOpen(false)} />
          <div className="winpick-panel">
            <label>From<input type="datetime-local" value={from}
                              onChange={(e) => setFrom(e.target.value)} /></label>
            <label>To<input type="datetime-local" value={to}
                            onChange={(e) => setTo(e.target.value)} /></label>
            {invalid && <span className="winpick-err">“To” must be after “From”.</span>}
            <div className="winpick-actions">
              <button className="btn-primary" disabled={invalid} onClick={apply}>Apply</button>
              <button onClick={() => setOpen(false)}>Cancel</button>
            </div>
          </div>
        </>
      )}
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

// ── histogram (distribution shape) ───────────────────────────────────────
// Counts per equal-width duration bin. Reads bimodality straight off the
// silhouette, which a box plot cannot show. Single series -> no legend.
export function Histogram({ bins, lo, hi, overflow, fmt }: {
  bins: number[]; lo: number; hi: number; overflow: number;
  fmt: (v: number) => string;
}) {
  const peak = Math.max(...bins, overflow, 1);
  const binW = (hi - lo) / Math.max(bins.length, 1);
  const cells = overflow > 0
    ? [...bins.map((c, i) => ({ c, i, over: false })), { c: overflow, i: bins.length, over: true }]
    : bins.map((c, i) => ({ c, i, over: false }));
  return (
    <div>
      <div className="hist">
        {cells.map(({ c, i, over }) => (
          <div key={i} className="hist-col"
               title={over
                 ? `slower than ${fmt(hi)} — ${overflow} execution${overflow === 1 ? "" : "s"}`
                 : `${fmt(lo + i * binW)} – ${fmt(lo + (i + 1) * binW)}\n${c} execution${c === 1 ? "" : "s"}`}>
            <div className={`hist-bar${over ? " over" : ""}`}
                 style={{ height: `${(c / peak) * 100}%` }} />
          </div>
        ))}
      </div>
      <div className="hist-axis">
        <span>{fmt(lo)}</span>
        <span className="muted">duration →</span>
        <span>{fmt(hi)}{overflow > 0 ? " +over" : ""}</span>
      </div>
    </div>
  );
}

// ── percentile band over time (drift) ────────────────────────────────────
// p25–p75 band with the median on top and p95 as a thin line: shows the
// spread MOVING, which neither the box plot nor the histogram can.
export function PercentileBand({ points, fmt }: {
  points: { t: number; p25: number; p50: number; p75: number; p95: number; n: number }[];
  fmt: (v: number) => string;
}) {
  // hooks before any early return (rules of hooks)
  const [hover, setHover] = useState<number | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  const W = 1000, H = 180, padL = 4, padR = 4;
  const n = points.length;
  const hi = Math.max(...points.map((p) => p.p95), 1);
  const x = (i: number) => padL + (n === 1 ? (W - padL - padR) / 2 : (i / (n - 1)) * (W - padL - padR));
  const y = (v: number) => H - (v / hi) * (H - 8) - 4;

  // snap to the nearest bucket as the pointer moves. The SVG is stretched
  // (preserveAspectRatio="none"), so work in fractions of the container
  // width rather than SVG units.
  const onMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const el = wrapRef.current;
    if (!el || n === 0) return;
    const r = el.getBoundingClientRect();
    const frac = (e.clientX - r.left) / Math.max(r.width, 1);
    const i = n === 1 ? 0 : Math.round(frac * (n - 1));
    setHover(Math.max(0, Math.min(n - 1, i)));
  };

  if (n === 0) return <div className="empty">No executions in this window.</div>;

  const path = (sel: (p: typeof points[0]) => number) =>
    points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(sel(p)).toFixed(1)}`).join(" ");
  const band = [
    ...points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.p75).toFixed(1)}`),
    ...[...points].reverse().map((p, j) => {
      const i = n - 1 - j;
      return `L${x(i).toFixed(1)},${y(p.p25).toFixed(1)}`;
    }),
    "Z",
  ].join(" ");

  const hp = hover != null ? points[hover] : null;
  const hpct = hover != null ? (x(hover) / W) * 100 : 0;
  const flip = hpct > 60; // keep the tooltip inside the card near the right edge

  return (
    <div>
      <div className="pband-wrap" ref={wrapRef}
           onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <svg className="pband" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
             style={{ height: H }}>
          <path d={band} fill="var(--grounded)" opacity="0.18" />
          <path d={path((p) => p.p95)} fill="none" stroke="var(--st-starved)" strokeWidth="2" />
          <path d={path((p) => p.p50)} fill="none" stroke="var(--grounded)" strokeWidth="2" />
          {hover != null && (
            <line x1={x(hover)} x2={x(hover)} y1={0} y2={H}
                  stroke="var(--secondary)" strokeWidth="1" strokeDasharray="3 3" />
          )}
          {points.map((p, i) => (
            <circle key={i} cx={x(i)} cy={y(p.p50)} r={i === hover ? 6 : 4}
                    fill="var(--grounded)"
                    stroke={i === hover ? "var(--strike)" : "none"} strokeWidth="2" />
          ))}
        </svg>
        {hp && (
          <div className={`pband-tip${flip ? " flip" : ""}`} style={{ left: `${hpct}%` }}>
            <b>{new Date(hp.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</b>
            <span className="muted"> · n={hp.n}</span>
            <div className="pband-tip-rows">
              <span>p95</span><b>{fmt(hp.p95)}</b>
              <span>p75</span><b>{fmt(hp.p75)}</b>
              <span>median</span><b>{fmt(hp.p50)}</b>
              <span>p25</span><b>{fmt(hp.p25)}</b>
            </div>
          </div>
        )}
      </div>
      <div className="gantt-axis">
        {[0, Math.floor((n - 1) / 2), n - 1].filter((i, k, a) => a.indexOf(i) === k).map((i) => (
          <span key={i}>
            {new Date(points[i].t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
          </span>
        ))}
      </div>
      <div className="bp-legend">
        <span><i className="bp-k-band" />p25–p75</span>
        <span><i className="bp-k-p50" />median</span>
        <span><i className="bp-k-p95" />p95</span>
        <span className="muted">peak {fmt(hi)}</span>
      </div>
    </div>
  );
}

// ── horizontal box plot ──────────────────────────────────────────────────
// One row per category. Box = p25..p75, rule = median, whiskers = p05..p95,
// tick = max. Single series, so no legend and no categorical palette: the
// title names the measure and the numbers are in the tooltip and the table.
// A shared linear scale across rows makes rows comparable at a glance.
export type BoxRow = {
  name: string;
  min: number; p05: number; p25: number; p50: number;
  p75: number; p95: number; max: number;
  n: number;
  detail?: string;
  flagged?: boolean; // draw the max tick in the down color (e.g. faulted)
};

export function BoxPlot({ rows, fmt, selected, onSelect }: {
  rows: BoxRow[];
  fmt: (v: number) => string;
  selected?: string;
  onSelect?: (name: string) => void;
}) {
  // Scale to the p95 envelope, NOT the max: one step that sat open across a
  // shutdown has a max in the tens of minutes and would flatten every box to
  // a hairline. Anything past the domain clamps to the right edge and is
  // marked, so the outlier is still visible without destroying the chart.
  const hi = Math.max(...rows.map((r) => r.p95), 1);
  const pc = (v: number) => `${Math.max(0, Math.min(100, (v / hi) * 100))}%`;
  const anyClamped = rows.some((r) => r.max > hi);
  return (
    <div className="boxplot">
      {rows.map((r) => {
        const on = r.name === selected;
        return (
          <div key={r.name}
               className={`bp-row${onSelect ? " clickable" : ""}${on ? " sel" : ""}`}
               onClick={onSelect ? () => onSelect(r.name) : undefined}
               title={`${r.name}  (n=${r.n})
min ${fmt(r.min)} · p05 ${fmt(r.p05)} · p25 ${fmt(r.p25)}
median ${fmt(r.p50)}
p75 ${fmt(r.p75)} · p95 ${fmt(r.p95)} · max ${fmt(r.max)}`}>
            <span className="bp-name">
              <b>{r.name}</b>
              {r.detail && <em title={r.detail}>{r.detail}</em>}
            </span>
            <div className="bp-track">
              {/* whisker p05..p95 */}
              <div className="bp-whisker"
                   style={{ left: pc(r.p05), width: pc(r.p95 - r.p05) }} />
              {/* box p25..p75 */}
              <div className="bp-box"
                   style={{ left: pc(r.p25), width: pc(Math.max(r.p75 - r.p25, 0)) }} />
              {/* median */}
              <div className="bp-median" style={{ left: pc(r.p50) }} />
              {/* max, so a timeout outlier is visible even when percentiles are tight */}
              <div className={`bp-max${r.flagged ? " bad" : ""}${r.max > hi ? " clamped" : ""}`}
                   style={{ left: pc(r.max) }} />
            </div>
            <span className="bp-val">{fmt(r.p50)}</span>
          </div>
        );
      })}
      <div className="bp-legend">
        <span><i className="bp-k-box" />p25–p75</span>
        <span><i className="bp-k-median" />median</span>
        <span><i className="bp-k-whisker" />p05–p95</span>
        <span><i className="bp-k-max" />max</span>
        <span className="muted">scale 0 – {fmt(hi)} (p95)</span>
        {anyClamped && (
          <span className="muted">
            <i className="bp-k-clamped" />max beyond scale — see tooltip
          </span>
        )}
      </div>
    </div>
  );
}

// ── horizontal bar list ──────────────────────────────────────────────────
// layout: default = truncated side label; wrap = wrapping side label;
// stacked = full text above the bar (best for long PLC reason strings).
export function Bars({ rows, wrap, stacked, valueFmt }: {
  rows: { name: string; value: number; color?: string; suffix?: string; detail?: string }[];
  wrap?: boolean;
  stacked?: boolean;
  valueFmt?: (v: number) => string;
}) {
  const max = Math.max(...rows.map((r) => r.value), 0.001);
  const cls = stacked ? "bars stacked" : wrap ? "bars wrap" : "bars";
  return (
    <div className={cls}>
      {rows.map((r, i) => (
        <div className="row" key={i}>
          <span className="name" title={r.detail ?? r.name}>{r.name}</span>
          {r.detail && stacked && r.detail !== r.name && (
            <span className="detail">{r.detail}</span>
          )}
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

// ── stacked vertical bars over time (flow reasons by bucket) ─────────────
export function StackedBars({
  labels, series, unit = " min",
}: {
  labels: string[];
  series: { name: string; color: string; values: number[] }[];
  unit?: string;
}) {
  if (!labels.length || !series.length) return <div className="empty">No data in this window.</div>;
  const width = 1000, height = 220, padL = 40, padB = 28, padT = 8;
  const totals = labels.map((_, i) => series.reduce((a, s) => a + (s.values[i] ?? 0), 0));
  const vmax = Math.max(...totals, 1) * 1.1;
  const plotW = width - padL - 8, plotH = height - padB - padT;
  const bw = plotW / labels.length;
  const y = (v: number) => padT + plotH - (v / vmax) * plotH;
  const labelEvery = Math.ceil(labels.length / 8);
  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", height }}>
        {[0.25, 0.5, 0.75, 1].map((f) => (
          <g key={f}>
            <line x1={padL} x2={width - 8} y1={y(vmax * f / 1.1)} y2={y(vmax * f / 1.1)}
                  stroke="var(--conduit)" strokeWidth={1} />
            <text x={padL - 6} y={y(vmax * f / 1.1) + 4} textAnchor="end"
                  fontSize={10} fill="var(--secondary)">
              {Math.round(vmax * f / 1.1)}{unit.trim()}
            </text>
          </g>
        ))}
        {labels.map((label, i) => {
          const x = padL + i * bw;
          let top = padT + plotH; // stack upward; first series sits on the baseline
          return (
            <g key={i}>
              {series.map((s) => {
                const v = s.values[i] ?? 0;
                if (v <= 0) return null;
                const h = (v / vmax) * plotH;
                top -= h;
                return (
                  <rect key={s.name} x={x + bw * 0.12} y={top} width={bw * 0.76}
                        height={Math.max(h, 0)} fill={s.color}>
                    <title>{`${label}\n${s.name}\n${v.toFixed(1)}${unit}`}</title>
                  </rect>
                );
              })}
              {i % labelEvery === 0 && (
                <text x={x + bw / 2} y={height - 8} textAnchor="middle"
                      fontSize={10} fill="var(--secondary)">{label}</text>
              )}
            </g>
          );
        })}
      </svg>
      <div className="gantt-legend">
        {series.map((s) => (
          <span key={s.name}><i style={{ background: s.color }} />{s.name}</span>
        ))}
      </div>
    </div>
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
