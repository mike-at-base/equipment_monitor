// Single-step widgets: one step of one EM, in detail.
//
// The step spread box plot answers "which step is slow?". These answer the
// two questions it raises but cannot show: what SHAPE is that step's
// distribution — one tight mode, or two? — and is it MOVING?

import { api, fmtMs, type WidgetScope } from "../api";
import {
  Histogram, PercentileBand, useAsync, usePolledAsync, useWindow,
} from "../components/ui";
import { Body, type WidgetProps } from "./frame";

function emOf(sc: WidgetScope): [string, string, string] {
  return [sc.line ?? "", sc.station ?? "", sc.em ?? ""];
}

/**
 * Resolves which step to chart.
 *
 * With none chosen the widget falls back to the slowest step in the window
 * rather than rendering nothing — a freshly added widget should show
 * something useful, and the slowest step is the one worth looking at first.
 * Which step it settled on is always named above the chart, so the fallback
 * is never silent.
 */
function useStep(scope: WidgetScope, w: WidgetProps["w"]) {
  const [l, s, e] = emOf(scope);
  const { win } = useWindow();
  const chosen = typeof w.opts?.step === "string" ? w.opts.step : "";
  const chosenSeq = Number(w.opts?.seq ?? 1);

  // only needed to resolve the fallback and to label the step
  const stats = useAsync(() => api.stepStats(l, s, e, win), [l, s, e, win]);
  const rows = stats.data?.steps ?? [];
  const row = chosen
    ? rows.find((r) => r.step === chosen && r.seq_index === chosenSeq)
    : rows[0]; // stepStats is slowest median first

  return {
    step: chosen || row?.step || "",
    seq: chosen ? chosenSeq : (row?.seq_index ?? 1),
    row,
    /** the chosen step ran nothing in this window */
    missing: !!chosen && !stats.err && !!stats.data && !row,
    loading: !stats.data && !stats.err,
    err: stats.err,
    usedFallback: !chosen && !!row,
  };
}

function StepHeading({ pick }: { pick: ReturnType<typeof useStep> }) {
  if (!pick.row) return null;
  return (
    <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
      <b>Step {pick.row.step}</b>
      {pick.row.description ? ` · ${pick.row.description}` : ""}
      {` · ${pick.row.count} runs · median ${fmtMs(pick.row.p50_ms)}`}
      {pick.usedFallback && " · slowest step (none chosen)"}
    </p>
  );
}

function useDetail(scope: WidgetScope, pick: ReturnType<typeof useStep>, bucket: string) {
  const [l, s, e] = emOf(scope);
  const { win } = useWindow();
  return usePolledAsync(
    () => (pick.step
      ? api.stepDetail(l, s, e, win, pick.step, pick.seq, bucket)
      : Promise.reject(new Error("no step"))),
    [l, s, e, win, pick.step, pick.seq, bucket]);
}

/** Guard shared by both widgets: nothing to chart, and why. */
function stepGate(pick: ReturnType<typeof useStep>) {
  if (pick.err) return <div className="empty">Could not load steps: {String(pick.err)}</div>;
  if (pick.loading) return <div className="empty">Loading…</div>;
  if (pick.missing) {
    return <div className="empty">Step “{pick.step}” did not run in this window.</div>;
  }
  if (!pick.step) return <div className="empty">No steps ran in this window.</div>;
  return null;
}

/** How long this step takes — the shape a box plot cannot show. */
export function StepDistribution({ w, scope }: WidgetProps) {
  const pick = useStep(scope, w);
  const q = useDetail(scope, pick, "auto");
  const gate = stepGate(pick);
  if (gate) return gate;
  return (
    <Body q={q} empty={(d) => d.histogram.bins.every((b) => b === 0) &&
                              "No runs of this step in the window."}>
      {(d) => {
        const h = d.histogram;
        const total = h.bins.reduce((a, b) => a + b, 0) + h.overflow;
        return (
          <>
            <StepHeading pick={pick} />
            <p className="muted" style={{ marginTop: 0, fontSize: 12 }}>
              {total} runs · bins to p95
              {h.overflow > 0 && ` · ${h.overflow} slower than ${fmtMs(h.hi_ms)}`}
            </p>
            <Histogram bins={h.bins} lo={h.lo_ms} hi={h.hi_ms} overflow={h.overflow}
                       fmt={(v) => fmtMs(v)} />
          </>
        );
      }}
    </Body>
  );
}

/** Whether that step is drifting, and whether its spread is widening. */
export function StepDrift({ w, scope }: WidgetProps) {
  const pick = useStep(scope, w);
  const bucket = String(w.opts?.bucket ?? "auto");
  const q = useDetail(scope, pick, bucket);
  const gate = stepGate(pick);
  if (gate) return gate;
  return (
    <Body q={q} empty={(d) => d.drift.length === 0 &&
                              "No runs of this step in the window."}>
      {(d) => (
        <>
          <StepHeading pick={pick} />
          <PercentileBand
            points={d.drift.map((p) => ({
              t: Date.parse(p.bucket_ts), n: p.count,
              p25: p.p25_ms, p50: p.p50_ms, p75: p.p75_ms, p95: p.p95_ms,
            }))}
            fmt={(v) => fmtMs(v)} />
          <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
            {d.bucket} buckets
          </p>
        </>
      )}
    </Body>
  );
}
