import { Link, useParams } from "react-router-dom";
import { api, ComposedResult, Interval, StationBand, winLabel } from "../api";
import { Bars, ErrorBox, Gantt, Loading, usePolledAsync, useWindow } from "../components/ui";

// Read-only composed-availability dashboard for one line: the headline
// number, per-station numbers, the composed up/down band, and top causes.
// No editors, no config — suitable for a shop-floor display.
export default function Availability() {
  const { line = "" } = useParams();
  const { win } = useWindow();
  const comp = usePolledAsync(() => api.lineComposed(line, win), [line, win]);

  if (comp.err) return <ErrorBox err={comp.err} />;
  if (!comp.data) return <Loading />;
  const d = comp.data;
  const c = d.composed;

  return (
    <>
      <div className="avdash-hero card">
        <div>
          <div className="avdash-big">
            {c.pct != null ? c.pct.toFixed(1) : "–"}<small>%</small>
          </div>
          <div className="avdash-sub">
            {line} composed availability ({winLabel(win)}) ·
            {" "}{c.production_min.toFixed(0)} min production time
            {c.default_model && " · default model (all stations in series)"}
          </div>
        </div>
      </div>

      <div className="avdash-grid">
        {Object.entries(d.stations).map(([name, pct]) => (
          <Link key={name} to={`/line/${line}/station/${name}`} className="avdash-station card">
            <div className="avdash-pct" style={{
              color: pct == null ? "var(--secondary)"
                : pct >= 85 ? "var(--grounded)"
                : pct >= 60 ? "var(--st-starved)" : "var(--st-down)",
            }}>
              {pct != null ? pct.toFixed(1) : "–"}<small>%</small>
            </div>
            <div className="avdash-name">{name}</div>
          </Link>
        ))}
      </div>

      <div className="card">
        <h2>Line availability band ({winLabel(win)})</h2>
        <Gantt rows={[{ label: line, intervals: composedIntervals(c) }]}
               from={Date.parse(d.from)} to={Date.parse(d.to)} />
        <OffShiftNote />
      </div>

      {(d.station_bands ?? []).length > 0 && (
        <div className="card">
          <div className="avdash-head">
            <h2>Station availability bands ({winLabel(win)})</h2>
            <span className="muted" style={{ fontSize: 13 }}>
              each station composed from its own modules · process order
            </span>
          </div>
          {/* The line band above says WHEN the line was down; this says which
              station took it there. Same composed maths, one level lower. */}
          <Gantt
            from={Date.parse(d.from)} to={Date.parse(d.to)}
            rows={(d.station_bands ?? []).slice(0, 15).map((b) => ({
              // no percentage here — the station tiles above already carry it
              label: b.station,
              intervals: bandIntervals(b),
            }))} />
          {(d.station_bands ?? []).length > 15 && (
            <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
              showing the first 15 of {(d.station_bands ?? []).length} stations
            </p>
          )}
          <OffShiftNote />
        </div>
      )}

      {(c.causes ?? []).length > 0 && (
        <div className="card">
          <h2>Unavailability by cause ({winLabel(win)})</h2>
          <Bars rows={(c.causes ?? []).slice(0, 10).map((r) => ({
            name: r.name, value: r.minutes, color: "var(--st-down)",
          }))} />
        </div>
      )}
    </>
  );
}

/** Gaps are non-production time, not missing data — the bands are clipped to
 *  the schedule so they describe the same time the percentages do. */
function OffShiftNote() {
  return (
    <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
      Clipped to scheduled production; gaps are time the line was not scheduled
      to run.
    </p>
  );
}

/** A station band in the shape Gantt wants, coloured like the line band. */
function bandIntervals(b: StationBand): Interval[] {
  const out: Interval[] = (b.up_spans ?? []).map((s) => ({
    start_ts: new Date(s.start).toISOString(),
    end_ts: new Date(s.end).toISOString(),
    state: "productive",
    reason: `${b.station} available`,
  }));
  for (const dn of (b.down ?? [])) {
    out.push({
      start_ts: dn.start_ts, end_ts: dn.end_ts, state: "down",
      reason: dn.causes.length ? `down: ${dn.causes.join(", ")}` : "down",
    });
  }
  return out;
}

function composedIntervals(c: ComposedResult): Interval[] {
  const out: Interval[] = (c.up_spans ?? []).map((s) => ({
    start_ts: new Date(s.start).toISOString(),
    end_ts: new Date(s.end).toISOString(),
    state: "productive",
    reason: "line available",
  }));
  for (const dn of (c.down ?? [])) {
    out.push({ start_ts: dn.start_ts, end_ts: dn.end_ts, state: "down",
               reason: dn.causes.join(", ") });
  }
  return out;
}
