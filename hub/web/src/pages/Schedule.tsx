import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, Shift } from "../api";
import { ErrorBox, Loading, useAsync } from "../components/ui";

const DOW = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

type Range = { start: string; end: string }; // HH:MM
const toMin = (hhmm: string) => {
  const [h, m] = hhmm.split(":").map(Number);
  return (h || 0) * 60 + (m || 0);
};
const toHHMM = (min: number) =>
  `${String(Math.floor(min / 60)).padStart(2, "0")}:${String(min % 60).padStart(2, "0")}`;

// Per-line weekly production schedule editor. Powers the E10 availability
// (production vs non-production time) and the "Prod today" window.
export default function Schedule() {
  const { line = "" } = useParams();
  const q = useAsync(() => api.getSchedule(line), [line]);
  const [days, setDays] = useState<Range[][]>(() => Array.from({ length: 7 }, () => []));
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState<unknown>();

  useEffect(() => {
    if (!q.data) return;
    const d: Range[][] = Array.from({ length: 7 }, () => []);
    for (const s of q.data.shifts) d[s.dow]?.push({ start: toHHMM(s.start_min), end: toHHMM(s.end_min) });
    setDays(d);
  }, [q.data]);

  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;

  const mut = (fn: (d: Range[][]) => void) => setDays((cur) => {
    const next = cur.map((r) => r.map((x) => ({ ...x })));
    fn(next);
    return next;
  });
  const addShift = (dow: number) => mut((d) => d[dow].push({ start: "06:00", end: "18:00" }));
  const removeShift = (dow: number, i: number) => mut((d) => d[dow].splice(i, 1));
  const setField = (dow: number, i: number, f: keyof Range, v: string) =>
    mut((d) => { d[dow][i][f] = v; });
  const copyToAll = (dow: number) => mut((d) => {
    const src = d[dow].map((x) => ({ ...x }));
    for (let k = 0; k < 7; k++) d[k] = src.map((x) => ({ ...x }));
  });

  const save = () => {
    setSaving(true); setSaved(false); setErr(undefined);
    const shifts: Shift[] = [];
    days.forEach((ranges, dow) => ranges.forEach((r) => {
      const start = toMin(r.start), end = toMin(r.end);
      if (end > start) shifts.push({ dow, start_min: start, end_min: end });
    }));
    api.saveSchedule(line, shifts).then(() => setSaved(true)).catch(setErr).finally(() => setSaving(false));
  };

  const totalShifts = days.reduce((a, r) => a + r.length, 0);
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2>Production schedule · {line}</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        Weekly production hours. Availability is measured over this scheduled
        time; hours outside it are non-production and don't count against
        availability. Times are local. Overnight shifts: enter as two rows
        (e.g. 22:00–24:00 and 00:00–06:00).
      </p>

      <div className="sched">
        {DOW.map((name, dow) => (
          <div className="sched-day" key={dow}>
            <div className="sched-dayhead">
              <span>{name}</span>
              {days[dow].length > 0 && (
                <button className="sched-copy" onClick={() => copyToAll(dow)}
                        title="Copy this day's shifts to every day">copy to all</button>
              )}
            </div>
            <div className="sched-shifts">
              {days[dow].length === 0 && <span className="muted" style={{ fontSize: 12 }}>no production</span>}
              {days[dow].map((r, i) => {
                const bad = toMin(r.end) <= toMin(r.start);
                return (
                  <div className={`sched-shift${bad ? " bad" : ""}`} key={i}>
                    <input type="time" value={r.start} onChange={(e) => setField(dow, i, "start", e.target.value)} />
                    <span>–</span>
                    <input type="time" value={r.end} onChange={(e) => setField(dow, i, "end", e.target.value)} />
                    <button className="chip-x" onClick={() => removeShift(dow, i)} aria-label="remove">×</button>
                  </div>
                );
              })}
              <button className="sched-add" onClick={() => addShift(dow)}>+ shift</button>
            </div>
          </div>
        ))}
      </div>

      <div style={{ marginTop: 16, display: "flex", gap: 12, alignItems: "center" }}>
        <button className="btn-primary" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save schedule"}</button>
        <span className="muted">{totalShifts} shift{totalShifts !== 1 ? "s" : ""}/week</span>
        {saved && <span style={{ color: "var(--st-productive)" }}>Saved ✓</span>}
        {err != null && <span style={{ color: "var(--st-down)" }}>Failed: {String(err)}</span>}
      </div>
    </div>
  );
}
