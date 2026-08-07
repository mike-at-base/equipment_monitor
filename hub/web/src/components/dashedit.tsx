// Dashboard editor controls: the scope picker, the widget options form, and
// the per-widget toolbar. Kept out of the page so the read-only render path
// stays short and obvious.

import { useState } from "react";
import type { HierLine, WidgetScope } from "../api";
import type { OptSpec, ScopeKind, WidgetDef } from "../widgets/registry";

// ── scope picker ──────────────────────────────────────────────────────────

/**
 * Picks an entity of one of `kinds`. When several kinds are allowed the user
 * chooses which; when only one is, the choice is implied and not shown.
 */
export function ScopePicker({ kinds, value, hier, onChange, allowInherit,
                              inheritLabel }: {
  kinds: ScopeKind[];
  value?: WidgetScope;
  hier: HierLine[];
  onChange: (sc: WidgetScope | undefined) => void;
  /** offer "inherit from dashboard" as an option */
  allowInherit?: boolean;
  /** what inheriting actually resolves to — "Inherit" alone tells you nothing */
  inheritLabel?: string;
}) {
  // a "none" widget (a note) has nothing to point at
  const usable = kinds.filter((k): k is Exclude<ScopeKind, "none"> => k !== "none");
  if (usable.length === 0) return null;
  const kind = value?.kind && (usable as ScopeKind[]).includes(value.kind)
    ? value.kind : usable[0];
  // Resolve every level against what is actually shown, so a partially
  // filled scope (or none at all) still renders a coherent set of selects
  // instead of an empty dropdown.
  const line = value?.line ?? hier[0]?.name ?? "";
  const stations = hier.find((l) => l.name === line)?.stations ?? [];
  const station = value?.station ?? stations[0]?.name ?? "";
  const ems = stations.find((s) => s.name === station)?.ems ?? [];
  const em = value?.em ?? ems[0]?.em_label ?? "";

  // patches carry the resolved values, so changing one select never leaves
  // the levels below it pointing at nothing
  const set = (patch: Partial<WidgetScope>) =>
    onChange({ ...value, kind, line, station, em, ...patch } as WidgetScope);

  return (
    <div className="dash-scopepick">
      {allowInherit && (
        <label>
          <span className="label">Scope</span>
          <select value={value ? "own" : "inherit"}
                  onChange={(e) => onChange(e.target.value === "inherit"
                    ? undefined
                    : { kind, line, ...defaultsFor(kind, hier, line) })}>
            <option value="inherit">
              {inheritLabel ? `Inherit — ${inheritLabel}` : "Inherit from dashboard"}
            </option>
            <option value="own">Choose equipment…</option>
          </select>
        </label>
      )}
      {(!allowInherit || value) && (
        <>
          {usable.length > 1 && (
            <label>
              <span className="label">Kind</span>
              <select value={kind} onChange={(e) => {
                const k = e.target.value as ScopeKind;
                // an EM list carries its own lines; a stray scope-level
                // `line` would just be dead weight in the saved spec
                onChange(k === "ems"
                  ? { kind: k, ems: [] }
                  : { kind: k, line, ...defaultsFor(k, hier, line) });
              }}>
                {usable.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
              </select>
            </label>
          )}
          {kind !== "ems" && (
            <label>
              <span className="label">Line</span>
              <select value={line} onChange={(e) => onChange({
                kind, line: e.target.value,
                ...defaultsFor(kind, hier, e.target.value),
              })}>
                {hier.map((l) => <option key={l.name} value={l.name}>{l.name}</option>)}
              </select>
            </label>
          )}
          {(kind === "station" || kind === "em") && (
            <label>
              <span className="label">Station</span>
              <select value={station} onChange={(e) => {
                const st = stations.find((s) => s.name === e.target.value);
                set({ station: e.target.value, em: kind === "em" ? st?.ems[0]?.em_label : undefined });
              }}>
                {stations.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
              </select>
            </label>
          )}
          {kind === "em" && (
            <label>
              <span className="label">EM</span>
              <select value={em} onChange={(e) => set({ em: e.target.value })}>
                {ems.map((m) => (
                  <option key={m.em_label} value={m.em_label}>
                    {m.em_label}{m.display_name ? ` · ${m.display_name}` : ""}
                  </option>
                ))}
              </select>
            </label>
          )}
          {kind === "ems" && (
            <EMMultiPicker hier={hier} selected={value?.ems ?? []}
                           onChange={(ems) => onChange({ kind: "ems", ems })} />
          )}
        </>
      )}
    </div>
  );
}

const KIND_LABEL: Record<string, string> = {
  line: "Line", station: "Station", em: "One EM", ems: "Several EMs",
};

// The first valid selection for a kind, so switching kind or line never
// leaves the scope pointing at nothing.
function defaultsFor(kind: ScopeKind, hier: HierLine[], line: string): Partial<WidgetScope> {
  const l = hier.find((h) => h.name === line);
  const st = l?.stations[0];
  switch (kind) {
    case "station": return { station: st?.name };
    case "em": return { station: st?.name, em: st?.ems[0]?.em_label };
    case "ems": return { ems: [] };
    default: return {};
  }
}

export const MAX_COMPARE_EMS = 40;

/** Every EM in the hierarchy, flattened, in tree order. */
export function flattenEMs(hier: HierLine[]): EMOption[] {
  return hier.flatMap((l) => l.stations.flatMap((s) => s.ems.map((m) => ({
    ref: `${l.name}/${s.name}/${m.em_label}`,
    line: l.name, station: s.name, label: m.em_label, display: m.display_name,
  }))));
}

export type EMOption = {
  ref: string; line: string; station: string; label: string; display: string;
};

/**
 * Multi-select over EVERY line's EMs, ordered as the user picks them.
 *
 * Comparing across lines is the point, so the picker deliberately does not
 * filter to one line. It does offer a line filter for the add-list, because
 * scrolling one <optgroup> of forty is worse than picking the line first —
 * but that is a view filter, never part of what gets saved.
 */
function EMMultiPicker({ hier, selected, onChange }: {
  hier: HierLine[]; selected: string[];
  onChange: (ems: string[]) => void;
}) {
  const [filter, setFilter] = useState("");
  const all = flattenEMs(hier);
  const byRef = new Map(all.map((o) => [o.ref, o]));
  const available = all.filter((o) => !selected.includes(o.ref)
    && (!filter || o.line === filter));
  const groups = [...new Set(available.map((o) => o.line))]
    .map((line) => ({ line, options: available.filter((o) => o.line === line) }));
  const full = selected.length >= MAX_COMPARE_EMS;
  const multiLine = new Set(selected.map((r) => r.split("/")[0])).size > 1;

  const add = (refs: string[]) =>
    onChange([...selected, ...refs.filter((r) => !selected.includes(r))]
      .slice(0, MAX_COMPARE_EMS));

  return (
    <div className="dash-emlist">
      <div className="dash-emhead">
        <span className="label">
          Selected EMs · {selected.length}
          {selected.length > 0 && " · drawn top to bottom in this order"}
        </span>
        {selected.length > 0 && (
          <button type="button" className="linkbtn"
                  onClick={() => onChange([])}>remove all</button>
        )}
      </div>

      {selected.length === 0 ? (
        <div className="dash-emempty">
          Nothing selected yet — this widget has nothing to draw.
          Pick equipment with <b>add an EM</b> below.
        </div>
      ) : (
        <ol className="dash-emrows">
          {selected.map((ref, i) => {
            const o = byRef.get(ref);
            return (
              <li key={ref} className={o ? "" : "gone"}>
                <span className="n">{i + 1}</span>
                {/* the line is only worth the space when the selection
                    actually spans more than one */}
                {multiLine && <span className="ln">{ref.split("/")[0]}</span>}
                <span className="who">
                  {o ? `${o.station}/${o.label}` : ref}
                  {o?.display && <em>{o.display}</em>}
                </span>
                {!o && <span className="warn">no longer configured</span>}
                <span className="sp" />
                <button type="button" className="iconbtn" title="move up"
                        disabled={i === 0}
                        onClick={() => onChange(swap(selected, i, i - 1))}>↑</button>
                <button type="button" className="iconbtn" title="move down"
                        disabled={i === selected.length - 1}
                        onClick={() => onChange(swap(selected, i, i + 1))}>↓</button>
                <button type="button" className="iconbtn danger" title="remove"
                        onClick={() => onChange(selected.filter((r) => r !== ref))}>×</button>
              </li>
            );
          })}
        </ol>
      )}

      <div className="dash-emadd">
        {hier.length > 1 && (
          <select value={filter} onChange={(e) => setFilter(e.target.value)}
                  aria-label="limit the add list to one line">
            <option value="">all lines</option>
            {hier.map((l) => <option key={l.name} value={l.name}>{l.name}</option>)}
          </select>
        )}
        <select value="" aria-label="add an EM" disabled={full}
                onChange={(e) => e.target.value && add([e.target.value])}>
          <option value="">add an EM…</option>
          {groups.map((g) => (
            <optgroup key={g.line} label={g.line}>
              {g.options.map((o) => (
                <option key={o.ref} value={o.ref}>
                  {o.station}/{o.label}{o.display ? ` · ${o.display}` : ""}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        {available.length > 0 && !full && (
          <button type="button" className="linkbtn"
                  onClick={() => add(available.map((o) => o.ref))}>
            add all {available.length}{filter && ` on ${filter}`}
          </button>
        )}
        {full && (
          <span className="muted" style={{ fontSize: 12 }}>
            {MAX_COMPARE_EMS} is the maximum.
          </span>
        )}
      </div>
    </div>
  );
}

function swap<T>(xs: T[], i: number, j: number): T[] {
  const out = xs.slice();
  [out[i], out[j]] = [out[j], out[i]];
  return out;
}

// ── options form ──────────────────────────────────────────────────────────

/** Renders a widget's options from its OptSpec list alone — no per-widget UI. */
export function OptsForm({ def, opts, onChange }: {
  def: WidgetDef;
  opts: Record<string, unknown>;
  onChange: (o: Record<string, unknown>) => void;
}) {
  if (!def.opts?.length) return null;
  const set = (k: string, v: unknown) => onChange({ ...opts, [k]: v });
  return (
    <div className="dash-opts">
      {def.opts.map((o: OptSpec) => (
        <label key={o.key}>
          <span className="label">{o.label}</span>
          {o.type === "select" && (
            <select value={String(opts[o.key] ?? o.def)}
                    onChange={(e) => set(o.key, e.target.value)}>
              {o.choices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
          )}
          {o.type === "number" && (
            <input type="number" min={o.min} max={o.max}
                   value={Number(opts[o.key] ?? o.def)}
                   onChange={(e) => set(o.key, clamp(Number(e.target.value), o.min, o.max))} />
          )}
          {o.type === "textarea" && (
            <textarea rows={3} value={String(opts[o.key] ?? o.def)}
                      onChange={(e) => set(o.key, e.target.value)} />
          )}
        </label>
      ))}
    </div>
  );
}

function clamp(v: number, lo: number, hi: number) {
  return Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : lo;
}

// ── add-widget catalogue ──────────────────────────────────────────────────

export function AddWidget({ defs, onAdd }: {
  defs: WidgetDef[]; onAdd: (d: WidgetDef) => void;
}) {
  const [open, setOpen] = useState(false);
  if (!open) {
    return (
      <button className="btn-primary" onClick={() => setOpen(true)}>Add widget</button>
    );
  }
  const groups = [...new Set(defs.map((d) => d.group))];
  return (
    <div className="card dash-catalogue">
      <div className="dash-cellhead">
        <h2>Add a widget</h2>
        <button className="linkbtn" onClick={() => setOpen(false)}>Close</button>
      </div>
      {groups.map((g) => (
        <div key={g} className="dash-cat-group">
          <div className="label">{g}</div>
          <div className="dash-cat-items">
            {defs.filter((d) => d.group === g).map((d) => (
              <button key={d.type} className="dash-cat-item"
                      aria-label={`Add ${d.title}`} title={d.blurb}
                      onClick={() => { onAdd(d); setOpen(false); }}>
                <b>{d.title}</b>
                <span className="muted">{d.blurb}</span>
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
