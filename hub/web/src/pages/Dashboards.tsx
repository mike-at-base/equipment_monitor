// The saved-dashboard list, and creating a new one.

import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type DashboardSpec } from "../api";
import { ErrorBox, Loading, useAsync, useWindow } from "../components/ui";

export default function DashboardsPage() {
  const { win } = useWindow();
  const q = useAsync(() => api.dashboards(), []);
  // carry the current window onto the dashboard link, same as every other
  // in-app navigation
  const qs = win ? `?win=${encodeURIComponent(win)}` : "";

  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;

  return (
    <>
      <div className="dash-head">
        <div>
          <div className="name">Dashboards</div>
          <div className="muted" style={{ fontSize: 13 }}>
            Saved views. Anyone with the link can edit or delete one.
          </div>
        </div>
      </div>

      {q.data.length === 0 ? (
        <div className="card"><div className="empty">No dashboards yet.</div></div>
      ) : (
        <div className="grid lines" style={{ marginTop: 16 }}>
          {q.data.map((d) => (
            <Link key={d.slug} to={`/d/${d.slug}${qs}`} className="card linecard">
              <div className="name">{d.name}</div>
              <div className="kvrow"><span>/d/{d.slug}</span></div>
              <div className="kvrow">
                <span>{d.author || "—"}</span>
                <span>{new Date(d.updated_at).toLocaleDateString()}</span>
              </div>
            </Link>
          ))}
        </div>
      )}

      <NewDashboard existing={q.data.map((d) => d.slug)} />
    </>
  );
}

function NewDashboard({ existing }: { existing: string[] }) {
  const nav = useNavigate();
  const [name, setName] = useState("");
  const [author, setAuthor] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const slug = slugify(name);
  const taken = existing.includes(slug);
  const ok = name.trim() !== "" && slug !== "" && !taken;

  const create = () => {
    setBusy(true); setErr("");
    const spec: DashboardSpec = { version: 1, widgets: [] };
    api.saveDashboard(slug, name.trim(), author.trim(), spec)
      .then(() => nav(`/d/${slug}`))
      .catch((e) => setErr(String(e)))
      .finally(() => setBusy(false));
  };

  return (
    <div className="card">
      <h2>New dashboard</h2>
      <div className="dash-meta">
        <label>
          <span className="label">Name</span>
          <input value={name} placeholder="CELL1 cycle time"
                 onChange={(e) => setName(e.target.value)} />
        </label>
        <label>
          <span className="label">Author (a label, not a login)</span>
          <input value={author} onChange={(e) => setAuthor(e.target.value)} />
        </label>
      </div>
      <p className="muted" style={{ fontSize: 13 }}>
        {slug ? <>URL will be <code>/d/{slug}</code></> : "The URL comes from the name."}
        {taken && <span style={{ color: "var(--st-down)" }}> — already taken</span>}
      </p>
      <button className="btn-primary" disabled={!ok || busy} onClick={create}>
        {busy ? "Creating…" : "Create"}
      </button>
      {err && <p style={{ color: "var(--st-down)" }}>{err}</p>}
    </div>
  );
}

/** Mirror of slugRe in internal/api/dashboards.go: lower-case, digits, dashes,
 *  starting and ending alphanumeric. */
function slugify(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "").slice(0, 63).replace(/-+$/, "");
}
