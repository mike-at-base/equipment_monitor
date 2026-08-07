// A saved dashboard: the widgets in the spec, laid out on a 4-column grid,
// plus an in-place editor.
//
// The time window is NOT part of the spec — it comes from the global picker,
// which lives in the URL, so /d/<slug>?win=8h is a complete shareable link.

import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api, winLabel,
  type Dashboard, type DashboardSpec, type DashWidget, type HierLine, type WidgetScope,
} from "../api";
import { AddWidget, OptsForm, ScopePicker } from "../components/dashedit";
import { ErrorBox, Loading, useAsync, useWindow } from "../components/ui";
import {
  defaultOpts, scopeIsEmpty, scopeLabel, widgetDef, WIDGETS, type WidgetDef,
} from "../widgets/registry";

export default function DashboardPage() {
  const { slug = "" } = useParams();
  const { win } = useWindow();
  // not polled: the spec only changes when someone edits it, and each widget
  // refreshes its own data on the usual cadence
  // bumped after a save so the view refetches the spec it just wrote
  const [rev, setRev] = useState(0);
  const q = useAsync(() => api.dashboard(slug), [slug, rev]);
  const hier = useAsync(() => api.hierarchy(), []);
  const [editing, setEditing] = useState(false);

  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;
  const d = q.data;

  if (editing) {
    return (
      <Editor dash={d} hier={hier.data ?? []}
              onDone={() => { setEditing(false); setRev((r) => r + 1); }}
              onCancel={() => setEditing(false)} />
    );
  }

  return (
    <>
      <div className="dash-head">
        <div>
          <div className="name">{d.name}</div>
          <div className="muted" style={{ fontSize: 13 }}>
            {winLabel(win)}
            {d.author && ` · ${d.author}`}
            {` · saved ${new Date(d.updated_at).toLocaleString()}`}
          </div>
        </div>
        <div className="actions">
          <button className="linkbtn" onClick={() => setEditing(true)}>Edit</button>
        </div>
      </div>
      <Grid spec={d.spec} />
    </>
  );
}

function Grid({ spec }: { spec: DashboardSpec }) {
  if (spec.widgets.length === 0) {
    return <div className="card"><div className="empty">This dashboard has no widgets yet.</div></div>;
  }
  return (
    <div className="dash-grid">
      {spec.widgets.map((w) => <Cell key={w.id} w={w} />)}
    </div>
  );
}

/**
 * One grid cell. Every widget carries its own scope, so there is nothing to
 * resolve — but it can still outlive the equipment it points at, or name a
 * type this build does not have, and either must degrade to a message rather
 * than break the page.
 */
export function Cell({ w, children }: {
  w: DashWidget; children?: React.ReactNode;
}) {
  const def = widgetDef(w.type);
  const needsEntity = !def || !def.scopes.includes("none");
  const scope = w.scope;
  const span = Math.min(4, Math.max(1, w.span || 1));
  const title = w.title || def?.title || w.type;

  let body;
  if (!def) {
    body = <div className="empty">Unknown widget type “{w.type}”.</div>;
  } else if (needsEntity && scopeIsEmpty(scope)) {
    body = <div className="empty">No equipment selected for this widget.</div>;
  } else {
    const { Render } = def;
    body = <Render w={w} scope={scope ?? { kind: "none" }} />;
  }

  return (
    <section className="card" style={{ gridColumn: `span ${span}` }}>
      <div className="dash-cellhead">
        <h2>{title}</h2>
        {w.scope && w.scope.kind !== "none" && (
          <span className="muted dash-scope">{scopeLabel(w.scope)}</span>
        )}
      </div>
      {children}
      {body}
    </section>
  );
}

// ── editor ────────────────────────────────────────────────────────────────

// Last write wins — there is no auth and no locking, so two people editing the
// same dashboard will overwrite each other. Said out loud in the UI rather
// than pretended away.
function Editor({ dash, hier, onDone, onCancel }: {
  dash: Dashboard; hier: HierLine[];
  onDone: () => void; onCancel: () => void;
}) {
  const nav = useNavigate();
  const [name, setName] = useState(dash.name);
  const [author, setAuthor] = useState(dash.author);
  const [spec, setSpec] = useState<DashboardSpec>(dash.spec);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  const mutate = (fn: (s: DashboardSpec) => DashboardSpec) => {
    setSpec((s) => fn(s));
    setDirty(true);
    setMsg("");
  };
  const setWidget = (id: string, patch: Partial<DashWidget>) =>
    mutate((s) => ({ ...s, widgets: s.widgets.map((w) => (w.id === id ? { ...w, ...patch } : w)) }));
  const move = (i: number, delta: number) =>
    mutate((s) => {
      const ws = s.widgets.slice();
      const j = i + delta;
      if (j < 0 || j >= ws.length) return s;
      [ws[i], ws[j]] = [ws[j], ws[i]];
      return { ...s, widgets: ws };
    });

  const save = () => {
    setSaving(true); setMsg("");
    api.saveDashboard(dash.slug, name, author, spec)
      .then(() => { setDirty(false); onDone(); })
      .catch((e) => setMsg(String(e)))
      .finally(() => setSaving(false));
  };
  const remove = () => {
    if (!confirm(`Delete dashboard "${dash.name}"? This cannot be undone.`)) return;
    api.deleteDashboard(dash.slug).then(() => nav("/d")).catch((e) => setMsg(String(e)));
  };

  return (
    <>
      <div className="dash-head">
        <div className="dash-meta">
          <label>
            <span className="label">Name</span>
            <input value={name} onChange={(e) => { setName(e.target.value); setDirty(true); }} />
          </label>
          <label>
            <span className="label">Author (a label, not a login)</span>
            <input value={author} onChange={(e) => { setAuthor(e.target.value); setDirty(true); }} />
          </label>
        </div>
        <div className="actions">
          <button className="btn-primary" disabled={saving || !dirty || !name.trim()}
                  onClick={save}>{saving ? "Saving…" : "Save"}</button>
          <button className="linkbtn" onClick={onCancel}>
            {dirty ? "Discard changes" : "Done"}
          </button>
          <button className="btn-danger" onClick={remove}>Delete</button>
        </div>
      </div>
      {msg && <div className="card" style={{ color: "var(--st-down)" }}>{msg}</div>}

      <p className="muted" style={{ fontSize: 13, margin: "10px 0 0" }}>
        Every widget picks its own equipment. Anyone with the link can edit or
        delete this dashboard; the last save wins.
      </p>

      <div style={{ marginTop: 14 }}>
        <AddWidget defs={WIDGETS} onAdd={(def) => mutate((s) => ({
          ...s,
          widgets: [...s.widgets, {
            id: newID(s.widgets), type: def.type, span: def.defaultSpan,
            // start on real equipment rather than an empty widget the user
            // has to go and fix before anything renders
            scope: firstScopeFor(def, hier),
            opts: defaultOpts(def),
          }],
        }))} />
      </div>

      {spec.widgets.length === 0 ? (
        <div className="card"><div className="empty">No widgets yet.</div></div>
      ) : (
        <div className="dash-grid">
          {spec.widgets.map((w, i) => (
            <Cell key={w.id} w={w}>
              <WidgetEditor w={w} hier={hier} first={i === 0}
                            last={i === spec.widgets.length - 1}
                            onMove={(d) => move(i, d)}
                            onChange={(patch) => setWidget(w.id, patch)}
                            onRemove={() => mutate((s) => ({
                              ...s, widgets: s.widgets.filter((x) => x.id !== w.id),
                            }))} />
            </Cell>
          ))}
        </div>
      )}
    </>
  );
}

function WidgetEditor({ w, hier, first, last, onMove, onChange, onRemove }: {
  w: DashWidget; hier: HierLine[];
  first: boolean; last: boolean;
  onMove: (delta: number) => void;
  onChange: (patch: Partial<DashWidget>) => void;
  onRemove: () => void;
}) {
  const def = widgetDef(w.type);
  const needsEntity = !def || !def.scopes.includes("none");
  return (
    <div className="dash-wedit">
      <div className="dash-wedit-row">
        <label>
          <span className="label">Title</span>
          <input value={w.title ?? ""} placeholder={def?.title ?? w.type}
                 onChange={(e) => onChange({ title: e.target.value })} />
        </label>
        <label>
          <span className="label">Width</span>
          <select value={w.span} onChange={(e) => onChange({ span: Number(e.target.value) })}>
            {[1, 2, 3, 4].map((n) => (
              <option key={n} value={n}>{n} of 4</option>
            ))}
          </select>
        </label>
        <span className="spacer" />
        <button className="linkbtn" disabled={first} onClick={() => onMove(-1)}>↑</button>
        <button className="linkbtn" disabled={last} onClick={() => onMove(1)}>↓</button>
        <button className="linkbtn dash-remove" onClick={onRemove}>Remove</button>
      </div>
      {def && (
        <>
          <ScopePicker kinds={def.scopes} value={w.scope} hier={hier}
                       onChange={(sc) => onChange({ scope: sc })} />
          <div className="dash-effective">
            {!needsEntity ? "No equipment needed."
              : scopeIsEmpty(w.scope)
                ? <span className="warn">No equipment selected — nothing will be drawn.</span>
                : <>Drawing <b>{scopeLabel(w.scope!)}</b></>}
          </div>
          <OptsForm def={def} opts={w.opts ?? {}} scope={w.scope}
                    onChange={(o) => onChange({ opts: o })} />
        </>
      )}
    </div>
  );
}

/**
 * A starting scope for a freshly added widget, so it renders real data
 * immediately instead of arriving broken and needing to be fixed first.
 * Multi-entity widgets start empty on purpose — guessing a set of EMs to
 * compare would be presumptuous, and the picker says what to do.
 */
function firstScopeFor(def: WidgetDef, hier: HierLine[]): WidgetScope | undefined {
  const kind = def.scopes[0];
  const line = hier[0];
  switch (kind) {
    case "none": return undefined;
    case "ems": return { kind: "ems", ems: [] };
    case "nodes": return { kind: "nodes", nodes: [] };
    case "line": return line ? { kind: "line", line: line.name } : undefined;
    case "station": {
      const st = line?.stations[0];
      return st ? { kind: "station", line: line.name, station: st.name } : undefined;
    }
    case "em": {
      const st = line?.stations.find((s) => s.ems.length > 0);
      const em = st?.ems[0];
      return em
        ? { kind: "em", line: line.name, station: st!.name, em: em.em_label }
        : undefined;
    }
    default: return undefined;
  }
}

// Widget ids only need to be unique within the spec.
function newID(ws: DashWidget[]): string {
  let n = ws.length + 1;
  const used = new Set(ws.map((w) => w.id));
  while (used.has(`w${n}`)) n++;
  return `w${n}`;
}
