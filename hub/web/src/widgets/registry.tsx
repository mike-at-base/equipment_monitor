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
  AvailabilityCompare, CycleCompare, LiveTiles, StateTimeline,
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
    blurb: "Ranked worst first over production time.",
    scopes: ["ems"], defaultSpan: 2, Render: AvailabilityCompare,
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

export function widgetDef(type: string): WidgetDef | undefined {
  return WIDGETS.find((d) => d.type === type);
}

/** Default opts for a freshly added widget, so the editor never saves nulls. */
export function defaultOpts(def: WidgetDef): Record<string, unknown> {
  const o: Record<string, unknown> = {};
  for (const s of def.opts ?? []) o[s.key] = s.def;
  return o;
}

/** Human label for a scope, for widget headers and the editor. */
export function scopeLabel(sc?: WidgetScope): string {
  if (!sc) return "inherited";
  switch (sc.kind) {
    case "none": return "";
    case "line": return sc.line ?? "";
    case "station": return `${sc.line}/${sc.station}`;
    case "em": return `${sc.station}/${sc.em}`;
    case "ems": return `${sc.ems?.length ?? 0} EMs on ${sc.line}`;
    default: return "";
  }
}
