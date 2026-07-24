import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, EMSummary, Interval, LiveEM, fmtSince, stateColor, STATE_LABEL } from "../api";
import { Bars, Gantt, Loading, ErrorBox, useAsync, useNow, useWindow } from "../components/ui";

// Line view — the SCADA mimic: EM tiles in config order with live state,
// step, dwell, reason; state gantt of the whole line below.
export default function Line({ live }: { live: LiveEM[] }) {
  const { line = "" } = useParams();
  const { win } = useWindow();
  const now = useNow();
  const sum = useAsync(() => api.summary(line, win), [line, win]);

  const liveByKey: Record<string, LiveEM> = {};
  for (const e of live.filter((e) => e.line === line)) {
    liveByKey[`${e.station}|${e.em_label}`.toLowerCase()] = e;
  }

  if (sum.err) return <ErrorBox err={sum.err} />;
  if (!sum.data) return <Loading />;
  const s = sum.data;

  return (
    <>
      <div className="tiles" style={{ marginTop: 16 }}>
        <Tile label={`Availability (${win})`}
          value={s.availability_pct != null ? `${s.availability_pct.toFixed(1)}` : "–"} unit="%" />
        <Tile label="Cycles" value={`${s.cycles.count}`} unit={s.cycles.per_hour ? ` · ${s.cycles.per_hour}/h` : ""} />
        <Tile label="Cycle p50" value={s.cycles.p50_ms != null ? (s.cycles.p50_ms / 1000).toFixed(1) : "–"} unit=" s" />
        <Tile label="Down" value={(s.state_min["down"] ?? 0).toFixed(1)} unit=" min" />
        <Tile label="Starved" value={(s.state_min["starved"] ?? 0).toFixed(1)} unit=" min" />
        <Tile label="Blocked" value={(s.state_min["blocked"] ?? 0).toFixed(1)} unit=" min" />
      </div>

      {groupByStation(s.ems).map(([station, ems]) => (
        <div className="station-group" key={station}>
          <div className="station-head">{station}</div>
          <div className="grid ems">
            {ems.map((em) => {
              const lv = liveByKey[`${em.station}|${em.em_label}`.toLowerCase()];
              const st = lv?.state || "no_data";
              return (
                <Link key={`${em.station}/${em.em_label}`}
                      to={`/em/${line}/${em.station}/${em.em_label}`}>
                  <div className={`emtile${em.confirmed ? "" : " unconfirmed"}`}>
                    <div className="head" style={{ background: stateColor(st) }}>
                      <span className="station" style={{ color: "var(--strike)" }}>
                        {em.display_name || em.em_label}
                        {em.em_label !== "main" && em.display_name ? "" : em.em_label !== "main" ? ` · ${em.em_label}` : ""}
                      </span>
                      <span className="st">{STATE_LABEL[st] ?? st}</span>
                    </div>
                    <div className="body">
                      {!em.confirmed && <span className="chip-unconfirmed">unconfirmed</span>}
                      <div className="step">{lv?.step ? `step ${lv.step}` : "—"}</div>
                      {lv?.since && <div className="dwell">in state {fmtSince(lv.since, now)}</div>}
                      {lv?.reason && <div className="reason" title={lv.reason}>{lv.reason}</div>}
                      <div className="kvrow" style={{ marginTop: 6 }}>
                        <span>avail</span>
                        <b>{em.availability_pct != null ? `${em.availability_pct.toFixed(1)}%` : "–"}</b>
                      </div>
                    </div>
                  </div>
                </Link>
              );
            })}
          </div>
        </div>
      ))}

      <LineGantt line={line} ems={s.ems.map((e) => ({ station: e.station, label: e.em_label }))} />

      {s.top_down_reasons.length > 0 && (
        <div className="card">
          <h2>Top down reasons ({win})</h2>
          <Bars rows={s.top_down_reasons.map((r) => ({
            name: `${r.reason} (×${r.count})`, value: r.minutes, color: "var(--st-down)",
          }))} />
        </div>
      )}
      {s.flow_losses.length > 0 && (
        <div className="card">
          <h2>Flow losses ({win})</h2>
          <Bars rows={s.flow_losses.map((f) => ({
            name: `${f.station} ${f.state} — ${f.top_reason}`, value: f.minutes,
            color: `var(--st-${f.state})`,
          }))} />
        </div>
      )}
    </>
  );
}

// group a line's EMs by their station, preserving first-seen order
function groupByStation(ems: EMSummary[]): [string, EMSummary[]][] {
  const m = new Map<string, EMSummary[]>();
  for (const em of ems) {
    const arr = m.get(em.station) ?? [];
    arr.push(em);
    m.set(em.station, arr);
  }
  return [...m.entries()];
}

function Tile({ label, value, unit }: { label: string; value: string; unit: string }) {
  return (
    <div className="tile">
      <div className="n">{value}<small>{unit}</small></div>
      <div className="label">{label}</div>
    </div>
  );
}

function LineGantt({ line, ems }: { line: string; ems: { station: string; label: string }[] }) {
  const [rows, setRows] = useState<{ label: string; intervals: Interval[] }[] | null>(null);
  const [err, setErr] = useState<unknown>();
  const to = Date.now();
  const from = to - 4 * 3600 * 1000;
  useEffect(() => {
    let liveFlag = true;
    Promise.all(ems.map((e) =>
      api.intervals(line, e.station, e.label, "4h")
        .then((iv) => ({ label: e.label === "main" ? e.station : `${e.station}·${e.label}`, intervals: iv }))))
      .then((r) => liveFlag && setRows(r))
      .catch((e) => liveFlag && setErr(e));
    return () => { liveFlag = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [line, ems.map((e) => e.station + e.label).join(",")]);
  if (err) return <ErrorBox err={err} />;
  return (
    <div className="card">
      <h2>State timeline (last 4 h)</h2>
      {rows ? <Gantt rows={rows} from={from} to={to} /> : <Loading />}
    </div>
  );
}
