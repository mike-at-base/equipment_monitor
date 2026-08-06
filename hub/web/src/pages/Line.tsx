import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, EMSummary, Interval, LiveEM, fmtClock, fmtSince, stateColor, STATE_LABEL } from "../api";
import { Bars, Gantt, Loading, ErrorBox, REFRESH_MS, usePolledAsync, useNow, useWindow } from "../components/ui";

// Line view — the SCADA mimic: EM tiles in config order with live state,
// step, dwell, reason; state gantt of the whole line below.
export default function Line({ live }: { live: LiveEM[] }) {
  const { line = "" } = useParams();
  const { win } = useWindow();
  const now = useNow();
  const sum = usePolledAsync(() => api.summary(line, win), [line, win]);
  const comp = usePolledAsync(() => api.lineComposed(line, win), [line, win]);

  const lineLive = live.filter((e) => e.line === line);
  const liveByKey: Record<string, LiveEM> = {};
  let liveDown = 0, liveStarved = 0, liveBlocked = 0;
  for (const e of lineLive) {
    liveByKey[`${e.station}|${e.em_label}`.toLowerCase()] = e;
    if (e.state === "down") liveDown++;
    else if (e.state === "starved") liveStarved++;
    else if (e.state === "blocked") liveBlocked++;
  }

  if (sum.err) return <ErrorBox err={sum.err} />;
  if (!sum.data) return <Loading />;
  const s = sum.data;

  return (
    <>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 16, marginTop: 12 }}>
        <Link to={`/line/${line}/availability`} className="linkbtn">Availability dashboard →</Link>
        <Link to={`/line/${line}/model`} className="linkbtn">Edit line availability model →</Link>
        <Link to={`/line/${line}/schedule`} className="linkbtn">Edit production schedule →</Link>
      </div>
      <div className="tiles" style={{ marginTop: 8 }}>
        <Tile label={`Composed availability (${win})`}
          value={s.availability_pct != null ? `${s.availability_pct.toFixed(1)}` : "–"} unit="%" />
        <Tile label={`Composed downtime (${win})`}
          value={s.episodes?.minutes != null ? s.episodes.minutes.toFixed(1) : "–"} unit=" min" />
        <Tile label="Live · down" value={`${liveDown}`} unit={` / ${lineLive.length} EMs`} />
        <Tile label="Live · starved" value={`${liveStarved}`} unit={` / ${lineLive.length} EMs`} />
        <Tile label="Live · blocked" value={`${liveBlocked}`} unit={` / ${lineLive.length} EMs`} />
      </div>

      {groupByStation(s.ems).map(([station, ems]) => (
        <div className="station-group" key={station}>
          <div className="station-head">
            <Link to={`/line/${line}/station/${station}`} className="station-link">{station}</Link>
            {comp.data?.stations?.[station] != null && (
              <span className="pct-chip" title="composed availability (redundancy model)">
                {comp.data.stations[station]!.toFixed(1)}%
              </span>
            )}
          </div>
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

      <LineGantt line={line} win={win} from={s.from} to={s.to}
        ems={s.ems.map((e) => ({ station: e.station, label: e.em_label }))} />

      {s.top_down_reasons.length > 0 && (
        <div className="card">
          <h2>Top composed down reasons ({win})</h2>
          <p className="muted" style={{ marginTop: 0, marginBottom: 8 }}>
            Wall-clock line downtime by sticky reason (k-of-n model). Concurrent identical
            reasons across EMs count once. ×N is how many distinct line-down stretches carried the reason.
          </p>
          <Bars stacked rows={s.top_down_reasons.map((r) => ({
            name: r.reason || "(no reason)",
            detail: `${r.reason_type || "fault"} · ×${r.count} stretch${r.count === 1 ? "" : "es"}`,
            value: r.minutes,
            color: "var(--st-down)",
          }))} />
        </div>
      )}
      {s.flow_losses.length > 0 && (
        <div className="card">
          <h2>Flow losses ({win})</h2>
          <p className="muted" style={{ marginTop: 0, marginBottom: 8 }}>
            Per-EM starved/blocked minutes (not composed). Useful to find which station is waiting —
            totals can overlap in time across EMs.
          </p>
          <Bars stacked rows={s.flow_losses.map((f) => ({
            name: `${f.station}${f.em_label !== "main" ? ` · ${f.em_label}` : ""} · ${f.state}`,
            detail: f.top_reason || "(no reason text)",
            value: f.minutes,
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

function LineGantt({ line, win, from, to, ems }: {
  line: string; win: string; from: string; to: string;
  ems: { station: string; label: string }[];
}) {
  const [rows, setRows] = useState<{ label: string; intervals: Interval[] }[] | null>(null);
  const [err, setErr] = useState<unknown>();
  const fromMs = Date.parse(from), toMs = Date.parse(to);
  useEffect(() => {
    let liveFlag = true;
    setRows(null);
    const load = () => Promise.all(ems.map((e) =>
      api.intervals(line, e.station, e.label, win)
        .then((iv) => ({ label: e.label === "main" ? e.station : `${e.station}·${e.label}`, intervals: iv }))))
      .then((r) => liveFlag && setRows(r))
      .catch((e) => liveFlag && setErr(e));
    load();
    const id = setInterval(load, REFRESH_MS); // keep the timeline current
    return () => { liveFlag = false; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [line, win, ems.map((e) => e.station + e.label).join(",")]);
  if (err) return <ErrorBox err={err} />;
  return (
    <div className="card">
      <h2>State timeline ({win})</h2>
      <p className="muted" style={{ marginTop: 0 }}>{fmtClock(from)} → {fmtClock(to)}</p>
      {rows ? <Gantt rows={rows} from={fromMs} to={toMs} /> : <Loading />}
    </div>
  );
}
