// Shared plumbing for dashboard widgets.
//
// The charts in components/ui.tsx render a degenerate frame rather than a
// message when they have no rows, and each page currently handles that its own
// way. On a dashboard that inconsistency is glaring — a grid of half-drawn
// axes. So loading, error and empty are normalised here, once, and a widget
// body only ever runs with data it can actually draw.

import type { ReactNode } from "react";
import type { DashWidget, WidgetScope } from "../api";
import { ErrorBox, Loading } from "../components/ui";

export type WidgetProps = {
  w: DashWidget;
  /** the widget's own scope — every widget carries one */
  scope: WidgetScope;
};

/**
 * Body renders `children(data)` once the query resolves.
 *
 * `empty` returns the message to show instead of the chart — a string when the
 * data is unusable, false when it is fine. Returning the message (rather than
 * a bare boolean) lets each widget say *what* is missing.
 */
export function Body<T>({ q, empty, children }: {
  q: { data?: T; err?: unknown };
  empty?: (d: T) => string | false;
  children: (d: T) => ReactNode;
}) {
  if (q.err) return <ErrorBox err={q.err} />;
  if (!q.data) return <Loading />;
  const msg = empty?.(q.data);
  if (msg) return <div className="empty">{msg}</div>;
  return <>{children(q.data)}</>;
}
