// Multi-EM comparison widgets — the capability no existing page has. All four
// read one /emcompare response, so adding an EM to a dashboard costs one row
// in one query rather than another round trip.

import {
  api, fmtMs, stateColor,
  type EMCompareRow, type Interval, type NodeCompareRow, type WidgetScope,
} from "../api";
import {
  Bars, BoxPlot, Gantt, GroupedStackedBars, SegmentBars, StackedBars, StateChip,
  usePolledAsync, useNow, useWindow,
} from "../components/ui";
import { Body, type WidgetProps } from "./frame";

function refs(sc: WidgetScope): string[] {
  return sc.ems ?? [];
}

function nodeRefs(sc: WidgetScope): string[] {
  return sc.nodes ?? [];
}

function useNodes(sc: WidgetScope) {
  const { win } = useWindow();
  const ns = nodeRefs(sc);
  return usePolledAsync(() => api.nodeCompare(ns, win), [ns.join(","), win]);
}

/** Node labels drop the line when every node is on the same one. */
function nodeLabeller(rows: NodeCompareRow[]): (r: NodeCompareRow) => string {
  const oneLine = new Set(rows.map((r) => r.line)).size <= 1;
  return (r) => (!r.station ? `${r.line} (whole line)`
    : oneLine ? r.station : r.ref);
}

const availColor = (pct: number) =>
  pct >= 85 ? "var(--grounded)"
    : pct >= 60 ? "var(--st-starved)" : "var(--st-down)";

const noNodes = "No stations or lines selected for this widget.";

// Row labels drop whatever the selection has in common: comparing four
// magazines on one station, "mag01" says everything; comparing the same press
// across two lines, the line is the only part that matters.
function labeller(rows: EMCompareRow[]): (r: EMCompareRow) => string {
  const oneLine = new Set(rows.map((r) => r.line)).size <= 1;
  const oneStation = oneLine && new Set(rows.map((r) => r.station)).size <= 1;
  return (r) => (oneStation ? r.em_label
    : oneLine ? `${r.station}/${r.em_label}`
    : r.ref);
}

function useCompare(sc: WidgetScope, intervals = false) {
  const { win } = useWindow();
  const ems = refs(sc);
  return usePolledAsync(() => api.emCompare(ems, win, intervals),
    [ems.join(","), win, intervals]);
}

const noEMs = "No EMs selected for this widget.";

/** Cycle time side by side, one box per EM. */
export function CycleCompare({ scope }: WidgetProps) {
  const q = useCompare(scope);
  return (
    <Body q={q} empty={(d) => (d.ems.length === 0 && noEMs) ||
                             (d.ems.every((e) => !e.spread) && "No cycles in this window.")}>
      {(d) => {
        const name = labeller(d.ems);
        return (
          <>
            <Missing refs={d.missing} />
            <BoxPlot
              rows={d.ems.filter((e) => e.spread).map((e) => {
                const s = e.spread!;
                return {
                  name: name(e), detail: `${s.count} cycles`,
                  min: s.min_ms, p05: s.p05_ms, p25: s.p25_ms, p50: s.p50_ms,
                  p75: s.p75_ms, p95: s.p95_ms, max: s.max_ms, n: s.count,
                };
              })}
              fmt={(v) => fmtMs(v)} />
            {d.ems.some((e) => !e.spread) && (
              <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
                No cycles: {d.ems.filter((e) => !e.spread).map(name).join(", ")}
              </p>
            )}
          </>
        );
      }}
    </Body>
  );
}

/**
 * Availability ranked worst first — the ordering that makes a gap obvious.
 *
 * Two different numbers, depending on what it is pointed at. EMs get each
 * module's own episode availability. Stations and lines get the COMPOSED
 * k-of-n number, which answers "could it run?" — a cell with one dead spare
 * magazine is still up, and averaging its modules would say otherwise.
 */
export function AvailabilityCompare(p: WidgetProps) {
  return p.scope.kind === "nodes"
    ? <ComposedAvailability {...p} />
    : <EMAvailability {...p} />;
}

function ComposedAvailability({ scope }: WidgetProps) {
  const q = useNodes(scope);
  return (
    <Body q={q} empty={(d) => (d.nodes.length === 0 && noNodes) ||
                              (d.nodes.every((n) => n.availability_pct == null) &&
                               "No production time in this window.")}>
      {(d) => {
        const name = nodeLabeller(d.nodes);
        const rows = d.nodes
          .filter((n) => n.availability_pct != null)
          .sort((a, b) => a.availability_pct! - b.availability_pct!);
        // a node with no saved model is everything-in-series, which reads
        // pessimistically next to one with real redundancy — say which
        const plain = rows.filter((n) => n.default_model && n.em_count > 1);
        return (
          <>
            <Missing refs={d.missing} />
            <Bars rows={rows.map((n) => ({
              name: name(n), value: n.availability_pct!,
              color: availColor(n.availability_pct!),
              detail: `${n.ref} · ${n.em_count} EMs · ${n.down_min} min down`,
            }))} valueFmt={(v) => `${v.toFixed(1)}%`} />
            <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
              Composed k-of-n availability over production time.
              {plain.length > 0 &&
                ` No redundancy model saved for ${plain.map(name).join(", ")} —
                  scored as everything in series.`}
            </p>
          </>
        );
      }}
    </Body>
  );
}

function EMAvailability({ scope }: WidgetProps) {
  const q = useCompare(scope);
  return (
    <Body q={q} empty={(d) => (d.ems.length === 0 && noEMs) ||
                              (d.ems.every((e) => e.availability_pct == null) &&
                               "No production time in this window.")}>
      {(d) => {
        const name = labeller(d.ems);
        const rows = d.ems
          .filter((e) => e.availability_pct != null)
          .sort((a, b) => a.availability_pct! - b.availability_pct!);
        return (
          <>
            <Missing refs={d.missing} />
            <Bars rows={rows.map((e) => ({
              name: name(e), value: e.availability_pct!,
              color: availColor(e.availability_pct!),
            }))} valueFmt={(v) => `${v.toFixed(1)}%`} />
            <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
              Each module on its own. Point this at stations instead for the
              composed number.
            </p>
            {rows.length < d.ems.length && (
              <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
                No production time: {d.ems.filter((e) => e.availability_pct == null)
                  .map(name).join(", ")}
              </p>
            )}
          </>
        );
      }}
    </Body>
  );
}

/** Every EM's state band on one time axis — where the losses line up. */
export function StateTimeline({ scope }: WidgetProps) {
  const q = useCompare(scope, true);
  return (
    <Body q={q} empty={(d) => (d.ems.length === 0 && noEMs) ||
                              (d.ems.every((e) => !e.intervals?.length) &&
                               "No state history in this window.")}>
      {(d) => {
        const name = labeller(d.ems);
        // Gantt squashes rather than scrolls, so cap the rows instead of
        // rendering an unreadable stripe per EM.
        const shown = d.ems.slice(0, 15);
        return (
          <>
            <Missing refs={d.missing} />
            <Gantt
              from={Date.parse(d.from)} to={Date.parse(d.to)}
              rows={shown.map((e) => ({
                label: name(e),
                // Gantt colours and labels states itself, so the API shape
                // goes straight through
                intervals: (e.intervals ?? []) as Interval[],
              }))} />
            {d.ems.length > shown.length && (
              <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
                showing the first {shown.length} of {d.ems.length} EMs
              </p>
            )}
          </>
        );
      }}
    </Body>
  );
}

/** Right-now state per EM. Polled faster than the aggregates — it is a
 *  glanceable tile, and a stale one is worse than useless. */
export function LiveTiles({ scope }: WidgetProps) {
  const wanted = new Set(refs(scope)); // already LINE/STATION/label
  const q = usePolledAsync(() => api.live(), [], 5000);
  const now = useNow();
  return (
    <Body q={q} empty={() => wanted.size === 0 && noEMs}>
      {(all) => {
        const rows = all.filter((e) => wanted.has(`${e.line}/${e.station}/${e.em_label}`));
        if (rows.length === 0) {
          return <div className="empty">None of these EMs are reporting.</div>;
        }
        return (
          <div className="dash-livetiles">
            {rows.map((e) => {
              // An EM that has stopped reporting comes back with an empty
              // state and a zero timestamp; both would render as garbage.
              const since = Date.parse(e.since);
              const held = e.state && since > 0 ? fmtDur(now - since) : "";
              return (
                <div key={`${e.station}/${e.em_label}`} className="tile">
                  <StateChip state={e.state || "no_data"} />
                  <div className="dash-lt-name">{e.station}/{e.em_label}</div>
                  <div className="muted" style={{ fontSize: 12 }}>
                    {e.reason || e.step || "—"}
                  </div>
                  {held && (
                    <div className="muted" style={{ fontSize: 12 }}>{held}</div>
                  )}
                </div>
              );
            })}
          </div>
        );
      }}
    </Body>
  );
}

// ── time by state, stacked ────────────────────────────────────────────────

const STATE_LABELS: Record<string, string> = {
  productive: "Productive", nva: "Non-value-added", standby: "Standby",
  process_wait: "Process wait", wait: "Waiting", starved: "Starved",
  blocked: "Blocked", paused: "Paused", manual: "Manual",
  offline: "Offline", down: "Down", no_data: "No data",
};
const stateName = (s: string) => STATE_LABELS[s] ?? s;

function bucketLabel(iso: string, bucket: string): string {
  const d = new Date(iso);
  const coarse = /^(4h|12h|1d)$/.test(bucket);
  return coarse
    ? d.toLocaleString([], { month: "numeric", day: "numeric", hour: "2-digit" })
    : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/**
 * Time by state as stacked columns.
 *
 * Two readings of the same question, chosen by the `axis` option. Over time
 * shows how a shift unfolded — the mix at 06:00 against the mix at 14:00. By
 * equipment shows how the same window treated each module. Percent mode is
 * the one to reach for when totals differ: it compares the MIX rather than
 * the amount, so a module that ran fewer hours is still readable.
 */
export function StateStackWidget({ w, scope }: WidgetProps) {
  const axis = String(w.opts?.axis ?? "time") === "equipment" ? "equipment" : "time";
  const percent = String(w.opts?.mode ?? "minutes") === "percent";
  const split = String(w.opts?.split ?? "combined") === "em";
  if (axis === "equipment") return <StateByEquipment scope={scope} w={w} percent={percent} />;
  return split
    ? <StateOverTimeSplit scope={scope} w={w} percent={percent} />
    : <StateOverTime scope={scope} w={w} percent={percent} />;
}

function StateOverTime({ scope, w, percent }: WidgetProps & { percent: boolean }) {
  const { win } = useWindow();
  const ems = refs(scope);
  const bucket = String(w.opts?.bucket ?? "auto");
  const q = usePolledAsync(() => api.stateStack(ems, win, bucket),
    [ems.join(","), win, bucket]);
  return (
    <Body q={q} empty={(d) => (ems.length === 0 && noEMs) ||
                              (d.series.length === 0 && "No state history in this window.")}>
      {(d) => (
        <>
          <StackedBars
            labels={d.buckets.map((b) => bucketLabel(b, d.bucket))}
            series={d.series.map((s) => ({
              name: stateName(s.state), color: stateColor(s.state), values: s.minutes,
            }))}
            percent={percent} />
          <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
            {d.bucket} buckets
            {/* only worth saying when the axis is minutes — in percent mode
                each column is normalised and the sum does not show */}
            {!percent && d.em_count > 1 && ` · ${d.em_count} EMs summed, so a
              column can exceed ${d.bucket} of clock time`}
          </p>
        </>
      )}
    </Body>
  );
}

/** One column per EM inside each time slice — who was doing what, and when. */
function StateOverTimeSplit({ scope, w, percent }: WidgetProps & { percent: boolean }) {
  const { win } = useWindow();
  const ems = refs(scope);
  const bucket = String(w.opts?.bucket ?? "auto");
  const q = usePolledAsync(() => api.stateStack(ems, win, bucket, true),
    [ems.join(","), win, bucket]);
  return (
    <Body q={q} empty={(d) => (ems.length === 0 && noEMs) ||
                              ((d.groups ?? []).every((g) => g.series.length === 0) &&
                               "No state history in this window.")}>
      {(d) => {
        const groups = d.groups ?? [];
        const oneStation = new Set(groups.map((g) => g.station)).size <= 1;
        // the stacking order has to be shared across groups, or the same
        // state would sit at a different height in each column
        const states = STATE_STACK_ORDER.filter((st) =>
          groups.some((g) => g.series.some((sr) => sr.state === st)));
        return (
          <>
            <GroupedStackedBars
              labels={d.buckets.map((b) => bucketLabel(b, d.bucket))}
              groups={groups.map((g) => ({
                name: oneStation ? g.em_label : `${g.station}/${g.em_label}`,
                values: Object.fromEntries(
                  g.series.map((sr) => [sr.state, sr.minutes])),
              }))}
              series={states.map((st) => ({
                key: st, label: stateName(st), color: stateColor(st),
              }))}
              percent={percent} />
            <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
              {d.bucket} slices · {groups.length} EMs
            </p>
          </>
        );
      }}
    </Body>
  );
}

function StateByEquipment({ scope, percent }: WidgetProps & { percent: boolean }) {
  // state_min per EM is already on the comparison response — no second endpoint
  const q = useCompare(scope);
  return (
    <Body q={q} empty={(d) => (d.ems.length === 0 && noEMs) ||
                              (d.ems.every((e) => Object.keys(e.state_min).length === 0) &&
                               "No state history in this window.")}>
      {(d) => {
        const name = labeller(d.ems);
        const states = STATE_STACK_ORDER.filter((st) =>
          d.ems.some((e) => (e.state_min[st] ?? 0) > 0));
        return (
          <>
            <Missing refs={d.missing} />
            <StackedBars
              labels={d.ems.map(name)}
              series={states.map((st) => ({
                name: stateName(st), color: stateColor(st),
                values: d.ems.map((e) => e.state_min[st] ?? 0),
              }))}
              percent={percent} />
          </>
        );
      }}
    </Body>
  );
}

// same order the API stacks in: good at the bottom, bad at the top
const STATE_STACK_ORDER = [
  "productive", "nva", "standby", "process_wait", "wait",
  "starved", "blocked", "paused", "manual", "offline", "down",
];

// ── flow loss: where the line waits rather than breaks ────────────────────

const FLOW_SEGMENTS = [
  { key: "starved", label: "Starved", color: stateColor("starved") },
  { key: "blocked", label: "Blocked", color: stateColor("blocked") },
  { key: "nva", label: "Non-value-added", color: stateColor("nva") },
];

/**
 * Starved / blocked / NVA time per station.
 *
 * Defaults to wall-clock minutes, because a line-wide starve is ONE outage
 * however many modules report it — summing them overstates elapsed time
 * several times over on a large cell. The summed figure is the right unit for
 * loss accounting, so it is one option away.
 */
export function FlowCompare({ w, scope }: WidgetProps) {
  const basis = String(w.opts?.basis ?? "wall") === "em" ? "em" : "wall";
  const q = useNodes(scope);
  return (
    <Body q={q} empty={(d) => (d.nodes.length === 0 && noNodes) ||
                              (d.nodes.every((n) => n.flow.length === 0) &&
                               "No starved, blocked or NVA time in this window.")}>
      {(d) => {
        const name = nodeLabeller(d.nodes);
        return (
          <>
            <Missing refs={d.missing} />
            <SegmentBars
              rows={d.nodes.map((n) => ({
                name: name(n), detail: n.ref,
                values: Object.fromEntries(n.flow.map((f) =>
                  [f.state, basis === "em" ? f.em_min : f.wall_min])),
              }))}
              segments={FLOW_SEGMENTS}
              fmt={(v) => `${Math.round(v)} min`}
              footnote={basis === "em"
                ? "Summed across each node's modules, so a wait affecting several counts once per module. Production time only."
                : "Clock time with at least one module waiting; concurrent waits counted once. Production time only."} />
          </>
        );
      }}
    </Body>
  );
}

/** Why the waiting happened, worst first — the pareto that names a cause. */
export function FlowReasons({ w, scope }: WidgetProps) {
  const top = Number(w.opts?.top ?? 10);
  const q = useNodes(scope);
  return (
    <Body q={q} empty={(d) => (d.nodes.length === 0 && noNodes) ||
                              (d.nodes.every((n) => n.flow_reasons.length === 0) &&
                               "No flow-loss reasons in this window.")}>
      {(d) => {
        const name = nodeLabeller(d.nodes);
        const many = d.nodes.length > 1;
        // one merged pareto across the selection; the node stays in the label
        // only when there is more than one to tell apart
        const merged = d.nodes.flatMap((n) => n.flow_reasons.map((r) => ({
          key: `${n.ref}|${r.state}|${r.reason}`,
          label: many ? `${name(n)} · ${r.reason}` : r.reason,
          state: r.state, minutes: r.minutes, count: r.count,
        }))).sort((a, b) => b.minutes - a.minutes).slice(0, top);
        return (
          <>
            <Missing refs={d.missing} />
            <Bars wrap rows={merged.map((r) => ({
              name: r.label, value: r.minutes, color: stateColor(r.state),
              detail: `${r.state} · ${r.count} occurrences`,
            }))} />
            <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
              Module-minutes, production time only.
            </p>
          </>
        );
      }}
    </Body>
  );
}

function fmtDur(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/** EMs the spec names that no longer exist — say so rather than drop them. */
function Missing({ refs }: { refs?: string[] }) {
  if (!refs?.length) return null;
  return (
    <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
      No longer configured: {refs.join(", ")}
    </p>
  );
}
