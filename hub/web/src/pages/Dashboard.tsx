// A saved dashboard: the widgets in the spec, laid out on a 4-column grid,
// plus an in-place editor.
//
// The time window is NOT part of the spec — it comes from the global picker,
// which lives in the URL, so /d/<slug>?win=8h is a complete shareable link.

import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api, winLabel,
  type Dashboard, type DashboardSpec, type DashWidget, type HierLine, type WidgetScope,
} from "../api";
import { AddWidget, OptsForm, ScopePicker } from "../components/dashedit";
import { ErrorBox, Loading, useAsync, useWindow } from "../components/ui";
import {
  DEFAULT_SCOPE_KINDS, defaultOpts, scopeIsEmpty, scopeLabel, widgetDef, WIDGETS,
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
            {d.spec.scope && ` · ${scopeLabel(d.spec.scope)}`}
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
      {spec.widgets.map((w) => <Cell key={w.id} w={w} fallback={spec.scope} />)}
    </div>
  );
}

/**
 * One grid cell. Resolves the widget's scope (its own, else the dashboard
 * default), looks up the renderer, and degrades to a message rather than
 * breaking the page when either is missing — a widget can outlive the EM it
 * points at, or the spec can name a type this build does not have.
 */
export function Cell({ w, fallback, children }: {
  w: DashWidget; fallback?: WidgetScope; children?: React.ReactNode;
}) {
  const def = widgetDef(w.type);
  // A widget that needs no entity does not inherit the dashboard scope —
  // same rule the server validates by.
  const needsEntity = !def || !def.scopes.includes("none");
  const scope = w.scope ?? (needsEntity ? fallback : undefined);
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
        {/* only when this widget overrides the dashboard default — otherwise
            it is the same string on every cell */}
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

  // A dashboard with no default scope leaves every inheriting widget pointing
  // at nothing, so seed one from the hierarchy. Not marked dirty: if the user
  // changes nothing there is nothing worth saving.
  useEffect(() => {
    if (spec.scope || hier.length === 0) return;
    const st = hier[0].stations[0];
    if (!st?.ems[0]) return;
    setSpec((s) => ({
      ...s,
      scope: { kind: "em", line: hier[0].name, station: st.name, em: st.ems[0].em_label },
    }));
  }, [hier, spec.scope]);

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

      <div className="card">
        <div className="dash-cellhead">
          <h2>Default scope</h2>
          <span className="muted" style={{ fontSize: 13 }}>
            widgets use this unless they choose their own
          </span>
        </div>
        {/* every kind any widget accepts — a default this picker cannot
            represent would be shown as another kind and overwritten on save */}
        <ScopePicker kinds={DEFAULT_SCOPE_KINDS} value={spec.scope}
                     hier={hier} allowInherit={false}
                     onChange={(sc) => mutate((s) => ({ ...s, scope: sc }))} />
        <p className="muted" style={{ marginBottom: 0, fontSize: 13 }}>
          Anyone with the link can edit or delete this dashboard; the last save wins.
        </p>
      </div>

      <div style={{ marginTop: 16 }}>
        <AddWidget defs={WIDGETS} onAdd={(def) => mutate((s) => ({
          ...s,
          widgets: [...s.widgets, {
            id: newID(s.widgets), type: def.type, span: def.defaultSpan,
            opts: defaultOpts(def),
          }],
        }))} />
      </div>

      {spec.widgets.length === 0 ? (
        <div className="card"><div className="empty">No widgets yet.</div></div>
      ) : (
        <div className="dash-grid">
          {spec.widgets.map((w, i) => (
            <Cell key={w.id} w={w} fallback={spec.scope}>
              <WidgetEditor w={w} hier={hier} fallback={spec.scope} first={i === 0}
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

function WidgetEditor({ w, hier, fallback, first, last, onMove, onChange, onRemove }: {
  w: DashWidget; hier: HierLine[]; fallback?: WidgetScope;
  first: boolean; last: boolean;
  onMove: (delta: number) => void;
  onChange: (patch: Partial<DashWidget>) => void;
  onRemove: () => void;
}) {
  const def = widgetDef(w.type);
  const needsEntity = !def || !def.scopes.includes("none");
  const effective = w.scope ?? (needsEntity ? fallback : undefined);
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
          <ScopePicker kinds={def.scopes} value={w.scope} hier={hier} allowInherit
                       inheritLabel={fallback ? scopeLabel(fallback) : undefined}
                       onChange={(sc) => onChange({ scope: sc })} />
          {/* say what this widget will actually draw, however the scope
              was arrived at */}
          <div className="dash-effective">
            {!needsEntity ? "No equipment needed."
              : scopeIsEmpty(effective)
                ? <span className="warn">No equipment selected — nothing will be drawn.</span>
                : <>Drawing <b>{scopeLabel(effective!)}</b></>}
          </div>
          <OptsForm def={def} opts={w.opts ?? {}} onChange={(o) => onChange({ opts: o })} />
        </>
      )}
    </div>
  );
}

// Widget ids only need to be unique within the spec.
function newID(ws: DashWidget[]): string {
  let n = ws.length + 1;
  const used = new Set(ws.map((w) => w.id));
  while (used.has(`w${n}`)) n++;
  return `w${n}`;
}
