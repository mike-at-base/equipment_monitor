// Multi-EM comparison widgets — the capability no existing page has. All four
// read one /emcompare response, so adding an EM to a dashboard costs one row
// in one query rather than another round trip.

import { api, fmtMs, type EMCompareRow, type Interval, type WidgetScope } from "../api";
import {
  Bars, BoxPlot, Gantt, StateChip, usePolledAsync, useNow, useWindow,
} from "../components/ui";
import { Body, type WidgetProps } from "./frame";

function refs(sc: WidgetScope): string[] {
  return sc.ems ?? [];
}

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

/** Availability ranked worst first — the ordering that makes a gap obvious. */
export function AvailabilityCompare({ scope }: WidgetProps) {
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
              color: e.availability_pct! >= 85 ? "var(--grounded)"
                : e.availability_pct! >= 60 ? "var(--st-starved)" : "var(--st-down)",
            }))} valueFmt={(v) => `${v.toFixed(1)}%`} />
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
