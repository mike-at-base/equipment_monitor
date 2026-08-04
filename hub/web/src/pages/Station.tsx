import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, AvailNode, ComposedResult, Interval, LiveEM, fmtClock } from "../api";
import { Bars, ErrorBox, Gantt, Loading, REFRESH_MS, usePolledAsync, useWindow } from "../components/ui";
import { ModelPanel, RBD } from "../components/availmodel";

// Station view — composed (k-of-n) availability: live RBD, composed
// timeline over the member EMs, cause pareto, and the model editor.
export default function Station({ live }: { live: LiveEM[] }) {
  const { line = "", station = "" } = useParams();
  const { win } = useWindow();
  const comp = usePolledAsync(() => api.stationComposed(line, station, win), [line, station, win]);

  const liveByKey: Record<string, LiveEM | undefined> = {};
  for (const e of live) {
    if (e.line === line && e.station.toLowerCase() === station.toLowerCase()) {
      liveByKey[e.em_label.toLowerCase()] = e;
    }
  }

  if (comp.err) return <ErrorBox err={comp.err} />;
  if (!comp.data) return <Loading />;
  const c = comp.data.composed;

  return (
    <>
      <div className="tiles" style={{ marginTop: 12 }}>
        <div className="tile">
          <div className="n">{c.pct != null ? c.pct.toFixed(1) : "–"}<small>%</small></div>
          <div className="label">Composed availability ({win})</div>
        </div>
        <div className="tile">
          <div className="n">{c.production_min.toFixed(0)}<small> min</small></div>
          <div className="label">Production time in window</div>
        </div>
        <div className="tile">
          <div className="n">{(c.down ?? []).length}<small></small></div>
          <div className="label">Composed outages</div>
        </div>
      </div>

      <div className="card">
        <h2>Redundancy model (live)</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Series groups must all be up; parallel groups need their threshold.
          {c.default_model && " No model configured — using the default (all EMs in series)."}
        </p>
        <div className="rbd">
          <RBD node={c.model} liveByKey={liveByKey} />
        </div>
      </div>

      <ComposedGantt line={line} station={station} win={win}
                     from={comp.data.from} to={comp.data.to} composed={c} />

      {(c.causes ?? []).length > 0 && (
        <div className="card">
          <h2>Composed unavailability by cause ({win})</h2>
          <p className="muted" style={{ marginTop: 0 }}>
            Only time where the STATION went down is charged. Concurrent causes
            (e.g. all mags down together) are each charged in full.
          </p>
          <Bars rows={(c.causes ?? []).map((r) => ({
            name: r.name, value: r.minutes, color: "var(--st-down)",
          }))} />
        </div>
      )}

      <div className="card">
        <h2>Availability model</h2>
        <ModelPanel memberNoun="EM"
          defaultHint={'all EMs in series. Customize to add redundancy (e.g. "any 1 of 4 mags").'}
          load={() => api.getStationModel(line, station)}
          save={(m) => api.saveStationModel(line, station, m)} />
      </div>
    </>
  );
}

// composed band mapped onto the state palette: up = productive green,
// down = down red (causes in the tooltip).
function composedIntervals(c: ComposedResult): Interval[] {
  const out: Interval[] = (c.up_spans ?? []).map((s) => ({
    start_ts: new Date(s.start).toISOString(),
    end_ts: new Date(s.end).toISOString(),
    state: "productive",
    reason: "station available",
  }));
  for (const d of (c.down ?? [])) {
    out.push({ start_ts: d.start_ts, end_ts: d.end_ts, state: "down",
               reason: d.causes.join(", ") });
  }
  return out;
}

function ComposedGantt({ line, station, win, from, to, composed }: {
  line: string; station: string; win: string; from: string; to: string;
  composed: ComposedResult;
}) {
  const [rows, setRows] = useState<{ label: string; intervals: Interval[] }[] | null>(null);
  const [err, setErr] = useState<unknown>();
  const members = leafNames(composed.model);
  useEffect(() => {
    let liveFlag = true;
    setRows(null);
    const load = () => Promise.all(members.map((em) =>
      api.intervals(line, station, em, win).then((iv) => ({ label: em, intervals: iv }))))
      .then((r) => liveFlag && setRows(r))
      .catch((e) => liveFlag && setErr(e));
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => { liveFlag = false; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [line, station, win, members.join(",")]);
  if (err) return <ErrorBox err={err} />;
  const fromMs = Date.parse(from), toMs = Date.parse(to);
  return (
    <div className="card">
      <h2>Composed timeline ({win})</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        {fmtClock(from)} → {fmtClock(to)} · top row is the station; green =
        available, red = composed down. Member outages that redundancy absorbed
        do NOT puncture the top row.
      </p>
      {rows ? (
        <Gantt rows={[{ label: `▶ ${station}`, intervals: composedIntervals(composed) }, ...rows]}
               from={fromMs} to={toMs} />
      ) : <Loading />}
    </div>
  );
}

function leafNames(n: AvailNode): string[] {
  if (n.em) return [n.em];
  if (n.station) return [n.station];
  return (n.children ?? []).flatMap(leafNames);
}

