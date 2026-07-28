import { useEffect, useState } from "react";
import { AvailModelResp, AvailNode, LiveEM, STATE_LABEL, stateColor } from "../api";
import { ErrorBox, Loading } from "./ui";

// States that count as "up" for composition — must mirror the server's
// availStates (manual/offline/no-data are composition-down).
const UP_STATES = new Set([
  "productive", "standby", "starved", "blocked", "process_wait", "wait", "paused",
]);

export function liveUp(node: AvailNode, liveByKey: Record<string, LiveEM | undefined>): boolean {
  if (node.em || node.station) {
    const lv = liveByKey[(node.em ?? node.station ?? "").toLowerCase()];
    return lv != null && UP_STATES.has(lv.state);
  }
  const kids = node.children ?? [];
  const need = node.k === "all" || node.k == null ? kids.length : node.k;
  let up = 0;
  for (const c of kids) if (liveUp(c, liveByKey)) up++;
  return up >= need;
}

// ── live reliability block diagram ───────────────────────────────────────
// ALL groups render as a series row; k-of-n groups stack children in
// parallel with a threshold badge. Blocks carry text labels + tooltips so
// state is never color-alone.
export function RBD({ node, liveByKey }: {
  node: AvailNode;
  liveByKey: Record<string, LiveEM | undefined>;
}) {
  if (node.em || node.station) {
    const key = node.em ?? node.station ?? "";
    const lv = liveByKey[key.toLowerCase()];
    const st = lv?.state ?? "no_data";
    return (
      <div className="rbd-leaf" style={{ background: stateColor(st) }}
           title={`${key} — ${STATE_LABEL[st] ?? st}${lv?.reason ? `\n${lv.reason}` : ""}`}>
        <span className="rbd-name">{key}</span>
        <span className="rbd-state">{STATE_LABEL[st] ?? st}</span>
      </div>
    );
  }
  const kids = node.children ?? [];
  const series = node.k === "all" || node.k == null;
  const need = series ? kids.length : (node.k as number);
  const upCount = kids.filter((c) => liveUp(c, liveByKey)).length;
  const ok = upCount >= need;
  return (
    <div className={`rbd-group ${series ? "series" : "parallel"} ${ok ? "ok" : "bad"}`}>
      <span className="rbd-badge" title={ok ? "requirement met" : "requirement NOT met"}>
        {series ? `all ${kids.length}` : `≥${need} of ${kids.length}`} · {upCount} up
      </span>
      <div className="rbd-kids">
        {kids.map((c, i) => <RBD key={i} node={c} liveByKey={liveByKey} />)}
      </div>
    </div>
  );
}

// ── model editor ─────────────────────────────────────────────────────────

// immutable update at a path of child indices
function updateAt(root: AvailNode, path: number[], fn: (n: AvailNode) => AvailNode | null): AvailNode | null {
  if (path.length === 0) return fn(root);
  const kids = [...(root.children ?? [])];
  const next = updateAt(kids[path[0]], path.slice(1), fn);
  if (next === null) kids.splice(path[0], 1);
  else kids[path[0]] = next;
  const k = root.k === "all" ? "all" : Math.min(root.k ?? kids.length, Math.max(kids.length, 1));
  return { ...root, k, children: kids };
}

export function ModelEditor({ root, members, memberNoun, onChange }: {
  root: AvailNode;
  members: string[];           // EM labels (station scope) or station names (line scope)
  memberNoun: string;          // "EM" | "station"
  onChange: (n: AvailNode) => void;
}) {
  return <GroupEd node={root} path={[]} root={root} members={members}
                  memberNoun={memberNoun} onChange={onChange} isRoot />;
}

function hasEmptyGroup(n: AvailNode): boolean {
  if (n.em || n.station) return false;
  const kids = n.children ?? [];
  return kids.length === 0 || kids.some(hasEmptyGroup);
}

// ModelPanel: load model + members, customize-from-default, edit, save,
// revert. Shared between the station (EMs) and line (stations) scopes.
export function ModelPanel({ memberNoun, defaultHint, load, save }: {
  memberNoun: string;
  defaultHint: string;
  load: () => Promise<AvailModelResp>;
  save: (m: AvailNode | null) => Promise<unknown>;
}) {
  const [resp, setResp] = useState<AvailModelResp | null>(null);
  const [draft, setDraft] = useState<AvailNode | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState<unknown>();

  useEffect(() => {
    load().then((r) => { setResp(r); setDraft(r.model); }).catch(setErr);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (err) return <ErrorBox err={err} />;
  if (!resp) return <Loading />;

  const doSave = (model: AvailNode | null) => {
    setSaving(true); setMsg("");
    save(model)
      .then(() => { setMsg("Saved ✓"); setDirty(false); setDraft(model); })
      .catch((e) => setMsg(`Failed: ${e}`))
      .finally(() => setSaving(false));
  };

  const invalid = draft != null && hasEmptyGroup(draft);
  return draft == null ? (
    <>
      <p className="muted" style={{ marginTop: 0 }}>
        Using the default: <b>{defaultHint}</b>
      </p>
      <button className="btn-primary"
              onClick={() => { setDraft(resp.default_model); setDirty(true); }}>
        Customize model
      </button>
    </>
  ) : (
    <>
      <ModelEditor root={draft} members={resp.members} memberNoun={memberNoun}
                   onChange={(n) => { setDraft(n); setDirty(true); }} />
      {invalid && <p style={{ color: "var(--st-down)" }}>Every group needs at least one member.</p>}
      <div style={{ marginTop: 12, display: "flex", gap: 12, alignItems: "center" }}>
        <button className="btn-primary" disabled={saving || invalid || !dirty}
                onClick={() => doSave(draft)}>{saving ? "Saving…" : "Save model"}</button>
        <button className="btn-danger" disabled={saving}
                onClick={() => doSave(null)}>Revert to default</button>
        {msg && <span className="muted">{msg}</span>}
      </div>
    </>
  );
}

function GroupEd({ node, path, root, members, memberNoun, onChange, isRoot }: {
  node: AvailNode; path: number[]; root: AvailNode; members: string[];
  memberNoun: string; onChange: (n: AvailNode) => void; isRoot?: boolean;
}) {
  const [pick, setPick] = useState("");
  const kids = node.children ?? [];
  const usedHere = new Set(kids.filter((c) => c.em || c.station).map((c) => c.em ?? c.station));
  const free = members.filter((m) => !usedHere.has(m));
  const mut = (p: number[], fn: (n: AvailNode) => AvailNode | null) => {
    const next = updateAt(root, p, fn);
    if (next) onChange(next);
  };
  const leafOf = (name: string): AvailNode =>
    memberNoun === "station" ? { station: name } : { em: name };

  return (
    <div className="avm-group">
      <div className="avm-head">
        <select value={node.k === "all" || node.k == null ? "all" : String(node.k)}
                onChange={(e) => mut(path, (n) => ({
                  ...n, k: e.target.value === "all" ? "all" : Number(e.target.value),
                }))}>
          <option value="all">require ALL (series)</option>
          {kids.length > 1 && Array.from({ length: kids.length - 1 }, (_, i) => i + 1).map((k) => (
            <option key={k} value={k}>
              {k === 1 ? "require ANY (≥1)" : `require at least ${k}`} of {kids.length}
            </option>
          ))}
        </select>
        {!isRoot && (
          <button className="chip-x" title="remove group (children are discarded)"
                  onClick={() => mut(path, () => null)}>×</button>
        )}
      </div>
      <div className="avm-kids">
        {kids.map((c, i) =>
          c.em || c.station ? (
            <span className="stepchip" key={`${c.em ?? c.station}-${i}`}>
              {c.em ?? c.station}
              <button className="chip-x" onClick={() => mut([...path, i], () => null)}
                      aria-label="remove">×</button>
            </span>
          ) : (
            <GroupEd key={`g${i}`} node={c} path={[...path, i]} root={root}
                     members={members} memberNoun={memberNoun} onChange={onChange} />
          ))}
      </div>
      <div className="avm-add">
        <select value={pick} onChange={(e) => {
          const v = e.target.value;
          setPick("");
          if (!v) return;
          mut(path, (n) => ({ ...n, children: [...(n.children ?? []), leafOf(v)] }));
        }}>
          <option value="">{`+ add ${memberNoun}…`}</option>
          {free.map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
        <button className="sched-add" onClick={() =>
          mut(path, (n) => ({
            ...n,
            children: [...(n.children ?? []), { k: 1, children: [] } as AvailNode],
          }))}>+ nested group</button>
      </div>
    </div>
  );
}
