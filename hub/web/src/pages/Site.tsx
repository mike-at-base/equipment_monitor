import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, LiveEM, Unconfirmed, STATE_ORDER, stateColor, STATE_LABEL } from "../api";
import { REFRESH_MS, usePolledAsync, useWindow, Loading, ErrorBox } from "../components/ui";

// Site overview: one card per line — live state strip + windowed KPIs.
export default function Site({ live }: { live: LiveEM[] }) {
  const { win } = useWindow();
  const lines = usePolledAsync(() => api.lines(), []);
  if (lines.err) return <ErrorBox err={lines.err} />;
  if (!lines.data) return <Loading />;
  return (
    <>
      <ReviewBanner />
      <div className="grid lines" style={{ marginTop: 16 }}>
        {lines.data.map((l) => (
          <LineCard key={l.name} name={l.name} emCount={l.em_count} win={win}
            live={live.filter((e) => e.line === l.name)} />
        ))}
      </div>
    </>
  );
}

// Auto-discovered EMs awaiting an engineer's review — confirm in the Config
// tab, or dismiss a phantom (e.g. a lineName typo) right here.
function ReviewBanner() {
  const [items, setItems] = useState<Unconfirmed[] | null>(null);
  const load = () => api.unconfirmed().then(setItems).catch(() => setItems([]));
  useEffect(() => {
    load();
    const id = setInterval(load, REFRESH_MS); // surface newly discovered EMs
    return () => clearInterval(id);
  }, []);
  if (!items || !items.length) return null;
  const dismiss = (u: Unconfirmed) => {
    if (!window.confirm(`Delete ${u.line} / ${u.station}${u.em_label !== "main" ? " · " + u.em_label : ""} and all its data? This can't be undone.`)) return;
    api.deleteEM(u.line, u.station, u.em_label).then(load);
  };
  return (
    <div className="reviewbanner" style={{ marginTop: 16 }}>
      <div className="rb-head">
        <span className="rb-count">{items.length}</span>
        <span>equipment module{items.length > 1 ? "s" : ""} discovered — review to confirm</span>
      </div>
      <div className="rb-list">
        {items.map((u, i) => (
          <div key={i} className="rb-item">
            <Link className="rb-info" to={`/em/${u.line}/${u.station}/${u.em_label}/config`}>
              <span><b>{u.line}</b> / {u.station}{u.em_label !== "main" ? ` · ${u.em_label}` : ""}</span>
              <span className="rb-go">review →</span>
            </Link>
            <button className="rb-dismiss" onClick={() => dismiss(u)}>dismiss</button>
          </div>
        ))}
      </div>
    </div>
  );
}

function LineCard({ name, emCount, win, live }: {
  name: string; emCount: number; win: string; live: LiveEM[];
}) {
  const sum = usePolledAsync(() => api.summary(name, win), [name, win]);
  const counts: Record<string, number> = {};
  for (const e of live) counts[e.state || "no_data"] = (counts[e.state || "no_data"] ?? 0) + 1;
  const total = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
  const s = sum.data;
  return (
    <Link to={`/line/${name}`}>
      <div className="card linecard" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <span className="name">{name}</span>
          <span className="muted">{emCount} EMs</span>
        </div>
        <div className="statebar" title={Object.entries(counts)
          .map(([k, v]) => `${STATE_LABEL[k] ?? k}: ${v}`).join("  ")}>
          {STATE_ORDER.filter((st) => counts[st]).map((st) => (
            <div key={st} style={{ flex: counts[st] / total, background: stateColor(st) }} />
          ))}
        </div>
        <div className="kvrow">
          <span>Availability ({win})</span>
          <b>{s?.availability_pct != null ? `${s.availability_pct.toFixed(1)}%` : "–"}</b>
        </div>
        <div className="kvrow">
          <span>Cycles</span>
          <b>{s ? `${s.cycles.count}${s.cycles.per_hour ? ` · ${s.cycles.per_hour}/h` : ""}` : "–"}</b>
        </div>
        <div className="kvrow">
          <span>Down</span>
          <b>{s ? `${(s.state_min["down"] ?? 0).toFixed(1)} min · ${s.mttr.downs} events` : "–"}</b>
        </div>
        <div className="kvrow">
          <span>Flow loss (starved+blocked)</span>
          <b>{s ? `${((s.state_min["starved"] ?? 0) + (s.state_min["blocked"] ?? 0)).toFixed(1)} min` : "–"}</b>
        </div>
      </div>
    </Link>
  );
}
