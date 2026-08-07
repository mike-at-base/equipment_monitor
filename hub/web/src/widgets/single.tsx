// Single-EM dashboard widgets. Each one fetches its own data and renders the
// chart body only — the card frame, title and empty/error states belong to the
// dashboard cell, so every widget looks the same when it has nothing to show.

import { api, fmtMs, fmtSec, type WidgetScope } from "../api";
import { BoxPlot, Histogram, PercentileBand, usePolledAsync, useWindow } from "../components/ui";
import { Body, type WidgetProps } from "./frame";

// A resolved single-EM scope, in the (line, station, em) order the API wants.
function emOf(sc: WidgetScope): [string, string, string] {
  return [sc.line ?? "", sc.station ?? "", sc.em ?? ""];
}

export function CycleKPIs({ scope }: WidgetProps) {
  const [l, s, e] = emOf(scope);
  const { win } = useWindow();
  const q = usePolledAsync(() => api.cycles(l, s, e, win), [l, s, e, win]);
  return (
    <Body q={q} empty={(d) => d.stats.count === 0 && "No cycles in this window."}>
      {(d) => (
        <div className="tiles">
          <Tile label="Cycles" v={`${d.stats.count}`} />
          <Tile label="Per hour" v={d.stats.per_hour?.toFixed(1) ?? "–"} />
          <Tile label="p10" v={fmtSec(d.stats.p10_ms)} />
          <Tile label="Median" v={fmtSec(d.stats.p50_ms)} />
          <Tile label="p90" v={fmtSec(d.stats.p90_ms)} />
          <Tile label="Interrupted" v={`${d.stats.interrupted}`} />
        </div>
      )}
    </Body>
  );
}

export function CycleDistribution({ w, scope }: WidgetProps) {
  const [l, s, e] = emOf(scope);
  const { win } = useWindow();
  const metric = String(w.opts?.metric ?? "total");
  const q = usePolledAsync(() => api.cycleDetail(l, s, e, win, metric),
    [l, s, e, win, metric]);
  return (
    <Body q={q} empty={(d) => d.histogram.bins.every((b) => b === 0) && "No cycles in this window."}>
      {(d) => {
        const h = d.histogram;
        const total = h.bins.reduce((a, b) => a + b, 0) + h.overflow;
        return (
          <>
            <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
              {total} cycles · bins to p95
              {h.overflow > 0 && ` · ${h.overflow} slower than ${fmtSec(h.hi_ms)}`}
            </p>
            <Histogram bins={h.bins} lo={h.lo_ms} hi={h.hi_ms} overflow={h.overflow}
                       fmt={(v) => fmtSec(v)} />
          </>
        );
      }}
    </Body>
  );
}

export function CycleDrift({ w, scope }: WidgetProps) {
  const [l, s, e] = emOf(scope);
  const { win } = useWindow();
  const metric = String(w.opts?.metric ?? "total");
  const bucket = String(w.opts?.bucket ?? "auto");
  const q = usePolledAsync(() => api.cycleDetail(l, s, e, win, metric, bucket),
    [l, s, e, win, metric, bucket]);
  return (
    <Body q={q} empty={(d) => d.drift.length === 0 && "No cycles in this window."}>
      {(d) => (
        <PercentileBand
          points={d.drift.map((p) => ({
            t: Date.parse(p.bucket_ts), n: p.count,
            p25: p.p25_ms, p50: p.p50_ms, p75: p.p75_ms, p95: p.p95_ms,
          }))}
          fmt={(v) => fmtSec(v)} />
      )}
    </Body>
  );
}

export function CycleSpread({ scope }: WidgetProps) {
  const [l, s, e] = emOf(scope);
  const { win } = useWindow();
  const q = usePolledAsync(() => api.cycleDetail(l, s, e, win, "total"), [l, s, e, win]);
  return (
    <Body q={q} empty={(d) => d.spread.length === 0 && "No cycles in this window."}>
      {(d) => (
        <BoxPlot
          rows={d.spread.map((r) => ({
            name: r.name, detail: `${r.count} cycles`,
            min: r.min_ms, p05: r.p05_ms, p25: r.p25_ms, p50: r.p50_ms,
            p75: r.p75_ms, p95: r.p95_ms, max: r.max_ms, n: r.count,
          }))}
          fmt={(v) => fmtSec(v)} />
      )}
    </Body>
  );
}

export function StepSpread({ w, scope }: WidgetProps) {
  const [l, s, e] = emOf(scope);
  const { win } = useWindow();
  const top = Number(w.opts?.top ?? 12);
  const q = usePolledAsync(() => api.stepStats(l, s, e, win), [l, s, e, win]);
  return (
    <Body q={q} empty={(d) => d.steps.length === 0 && "No step history in this window."}>
      {(d) => {
        const multiSeq = new Set(d.steps.map((r) => r.seq_index)).size > 1;
        return (
          <>
            <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
              slowest median first · {Math.min(top, d.steps.length)} of {d.steps.length} steps
            </p>
            <BoxPlot
              rows={d.steps.slice(0, top).map((r) => ({
                name: multiSeq ? `${r.seq_index}·${r.step}` : r.step,
                detail: r.description,
                min: r.min_ms, p05: r.p05_ms, p25: r.p25_ms, p50: r.p50_ms,
                p75: r.p75_ms, p95: r.p95_ms, max: r.max_ms, n: r.count,
                flagged: r.faulted > 0,
              }))}
              fmt={(v) => fmtMs(v)} />
          </>
        );
      }}
    </Body>
  );
}

// Free text. No fetch, so no Body wrapper.
export function Note({ w }: WidgetProps) {
  const text = String(w.opts?.text ?? "");
  if (!text.trim()) return <div className="empty">Empty note.</div>;
  return <div className="dash-note">{text}</div>;
}

function Tile({ label, v }: { label: string; v: string }) {
  return (
    <div className="tile">
      <div className="n">{v}</div>
      <div className="label">{label}</div>
    </div>
  );
}
