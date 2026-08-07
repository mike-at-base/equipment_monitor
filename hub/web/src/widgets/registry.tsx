// The widget catalogue — the "pre-configured list of widgets" the dashboard
// editor offers. Adding a widget means adding one entry here plus its adapter;
// neither the editor nor the renderer needs to change.
//
// `scopes` must stay in step with widgetTypes in internal/api/dashboards.go,
// which enforces the same rule server-side so a hand-rolled spec cannot be
// saved with a widget pointed at the wrong kind of entity.

import type { ComponentType } from "react";
import type { WidgetScope } from "../api";
import type { WidgetProps } from "./frame";
import {
  AvailabilityCompare, CycleCompare, FlowCompare, FlowReasons, LiveTiles,
  StateTimeline,
} from "./compare";
import {
  CycleDistribution, CycleDrift, CycleKPIs, CycleSpread, Note, StepSpread,
} from "./single";

export type ScopeKind = WidgetScope["kind"];

/** An option the editor renders generically from this description alone. */
export type OptSpec =
  | { key: string; label: string; type: "select"; def: string;
      choices: { value: string; label: string }[] }
  | { key: string; label: string; type: "number"; def: number; min: number; max: number }
  | { key: string; label: string; type: "textarea"; def: string };

export type WidgetDef = {
  type: string;
  title: string;
  /** one line in the "add widget" list explaining what it answers */
  blurb: string;
  group: "Cycle time" | "Availability" | "Steps" | "Other";
  scopes: ScopeKind[];
  defaultSpan: number;
  opts?: OptSpec[];
  Render: ComponentType<WidgetProps>;
};

const metricOpt: OptSpec = {
  key: "metric", label: "Metric", type: "select", def: "total",
  choices: [
    { value: "total", label: "Total" },
    { value: "work", label: "Work" },
    { value: "exchange", label: "Exchange" },
  ],
};

export const WIDGETS: WidgetDef[] = [
  // multi-EM comparisons first: they are the reason custom dashboards exist,
  // and nothing else in the app can show them
  {
    type: "cycle_compare", title: "Cycle time compare", group: "Cycle time",
    blurb: "One box plot row per EM — who is slow, and how consistently?",
    scopes: ["ems"], defaultSpan: 4, Render: CycleCompare,
  },
  {
    type: "availability_compare", title: "Availability compare", group: "Availability",
    blurb: "Ranked worst first. Stations and lines give the composed k-of-n number; EMs give each module on its own.",
    scopes: ["nodes", "ems"], defaultSpan: 2, Render: AvailabilityCompare,
  },
  {
    type: "flow_compare", title: "Flow loss by station", group: "Availability",
    blurb: "Starved, blocked and NVA time per station — where the line waits rather than breaks.",
    scopes: ["nodes"], defaultSpan: 2,
    opts: [{
      key: "basis", label: "Measured as", type: "select", def: "wall",
      choices: [
        { value: "wall", label: "Clock time (concurrent waits counted once)" },
        { value: "em", label: "Module-minutes (summed across modules)" },
      ],
    }],
    Render: FlowCompare,
  },
  {
    type: "flow_reasons", title: "Flow loss reasons", group: "Availability",
    blurb: "Why the waiting happened, worst first.",
    scopes: ["nodes"], defaultSpan: 2,
    opts: [{ key: "top", label: "Reasons shown", type: "number", def: 10, min: 3, max: 25 }],
    Render: FlowReasons,
  },
  {
    type: "state_timeline", title: "State timeline", group: "Availability",
    blurb: "Every EM's state band on one axis — do the losses line up?",
    scopes: ["ems"], defaultSpan: 4, Render: StateTimeline,
  },
  {
    type: "live_tiles", title: "Live state tiles", group: "Availability",
    blurb: "What each EM is doing right now.",
    scopes: ["ems"], defaultSpan: 2, Render: LiveTiles,
  },
  {
    type: "cycle_kpis", title: "Cycle KPI tiles", group: "Cycle time",
    blurb: "Count, rate and p10/median/p90 for the window.",
    scopes: ["em"], defaultSpan: 4, Render: CycleKPIs,
  },
  {
    type: "cycle_distribution", title: "Cycle distribution", group: "Cycle time",
    blurb: "Histogram — one tight mode, or several?",
    scopes: ["em"], defaultSpan: 2, opts: [metricOpt], Render: CycleDistribution,
  },
  {
    type: "cycle_drift", title: "Cycle drift over time", group: "Cycle time",
    blurb: "p25–p95 band per bucket — is the spread moving?",
    scopes: ["em"], defaultSpan: 2,
    opts: [metricOpt, {
      key: "bucket", label: "Bucket", type: "select", def: "auto",
      choices: ["auto", "1m", "5m", "15m", "1h"].map((v) => ({ value: v, label: v })),
    }],
    Render: CycleDrift,
  },
  {
    type: "cycle_spread", title: "Cycle spread", group: "Cycle time",
    blurb: "Box plot of total against its work and exchange phases.",
    scopes: ["em"], defaultSpan: 2, Render: CycleSpread,
  },
  {
    type: "step_spread", title: "Step duration spread", group: "Steps",
    blurb: "Box plot per step, slowest median first.",
    scopes: ["em"], defaultSpan: 4,
    opts: [{ key: "top", label: "Steps shown", type: "number", def: 12, min: 3, max: 40 }],
    Render: StepSpread,
  },
  {
    type: "note", title: "Note", group: "Other",
    blurb: "Free text — context for whoever opens the dashboard.",
    scopes: ["none"], defaultSpan: 4,
    opts: [{ key: "text", label: "Text", type: "textarea", def: "" }],
    Render: Note,
  },
];

/**
 * Every scope kind some widget can be pointed at, in display order.
 *
 * The dashboard's default-scope picker MUST offer all of them: a default it
 * cannot represent gets shown as some other kind and silently overwritten on
 * save. Derived from the registry so adding a widget with a new scope kind
 * cannot leave the picker behind.
 */
export const DEFAULT_SCOPE_KINDS: ScopeKind[] =
  (["nodes", "ems", "em", "station", "line"] as ScopeKind[])
    .filter((k) => WIDGETS.some((d) => d.scopes.includes(k)));

export function widgetDef(type: string): WidgetDef | undefined {
  return WIDGETS.find((d) => d.type === type);
}

/** Default opts for a freshly added widget, so the editor never saves nulls. */
export function defaultOpts(def: WidgetDef): Record<string, unknown> {
  const o: Record<string, unknown> = {};
  for (const s of def.opts ?? []) o[s.key] = s.def;
  return o;
}

/** True when a scope names no equipment, so a widget using it draws nothing. */
export function scopeIsEmpty(sc?: WidgetScope): boolean {
  if (!sc) return true;
  if (sc.kind === "ems") return (sc.ems?.length ?? 0) === 0;
  if (sc.kind === "nodes") return (sc.nodes?.length ?? 0) === 0;
  return false;
}

/** Human label for a scope, for widget headers and the editor. */
export function scopeLabel(sc?: WidgetScope): string {
  if (!sc) return "inherited";
  switch (sc.kind) {
    case "none": return "";
    case "line": return sc.line ?? "";
    case "station": return `${sc.line}/${sc.station}`;
    case "em": return `${sc.station}/${sc.em}`;
    case "nodes": {
      const refs = sc.nodes ?? [];
      if (refs.length === 0) return "no stations selected";
      const n = refs.length === 1 ? refs[0]
        : `${refs.length} stations/lines`;
      return n;
    }
    case "ems": {
      const refs = sc.ems ?? [];
      if (refs.length === 0) return "no EMs selected";
      const n = `${refs.length} EM${refs.length === 1 ? "" : "s"}`;
      const lines = [...new Set(refs.map((r) => r.split("/")[0]))];
      return lines.length === 1 ? `${n} on ${lines[0]}`
        : `${n} across ${lines.length} lines`;
    }
    default: return "";
  }
}
