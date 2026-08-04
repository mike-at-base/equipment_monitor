import { Link, useParams } from "react-router-dom";
import { api, ComposedResult, Interval } from "../api";
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
            {line} composed availability ({win}) ·
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
        <h2>Line availability band ({win})</h2>
        <Gantt rows={[{ label: line, intervals: composedIntervals(c) }]}
               from={Date.parse(d.from)} to={Date.parse(d.to)} />
      </div>

      {(c.causes ?? []).length > 0 && (
        <div className="card">
          <h2>Unavailability by cause ({win})</h2>
          <Bars rows={(c.causes ?? []).slice(0, 10).map((r) => ({
            name: r.name, value: r.minutes, color: "var(--st-down)",
          }))} />
        </div>
      )}
    </>
  );
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
