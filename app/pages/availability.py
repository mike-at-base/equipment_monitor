"""
Availability page — SEMI E10 four-state model.

State derivation (SQL CASE on em_availability_raw signals):
  productive       — automatic=T, running=T, fault=F   → Livewire green
  standby          — automatic=T, running=F, fault=F   → Goldenrod
  unscheduled_down — fault=T (any mode)                → Red
  manual           — automatic=F, fault=F              → Charcoal (excluded from %)

Availability % = (productive_s + standby_s)
               / (productive_s + standby_s + down_s) × 100
Manual time is excluded from the denominator (SEMI E10 Non-Scheduled Time).
"""
from __future__ import annotations

import datetime
from collections import defaultdict

import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
from dash import dash_table, dcc, html

import db.queries as q
from app.brand import AVAIL_PCT_COND, DT_STYLE_CELL, DT_STYLE_HEADER, DT_STYLE_TABLE

# ── SEMI E10 visual config ────────────────────────────────────────────────────

STATE_COLOR = {
    "productive":        "#b2dd79",  # Livewire
    "standby":           "#f7c33c",  # Goldenrod
    "unscheduled_down":  "#c51808",  # Red-80
    "manual":            "#3a3733",  # Charcoal
}
STATE_LABEL = {
    "productive":        "Productive",
    "standby":           "Standby",
    "unscheduled_down":  "Faulted (down)",
    "manual":            "Manual / Off",
}
STATE_ORDER = ["productive", "standby", "unscheduled_down", "manual"]


def render(em_ids: list[int], start: datetime.datetime, end: datetime.datetime) -> html.Div:
    if not em_ids:
        return html.Div("Select at least one station.", className="text-muted p-3")

    summary_df  = q.query_state_summary(em_ids, start, end)
    timeline_df = q.query_state_timeline(em_ids, start, end)

    # ── Summary table ─────────────────────────────────────────────────────────
    if summary_df.empty:
        summary_section = html.Div("No availability data yet.", className="text-muted")
    else:
        disp = summary_df[[
            "station", "display_name", "em_label",
            "availability_pct", "productive_min", "standby_min",
            "down_min", "manual_min",
        ]].copy()
        disp = disp.rename(columns={
            "station":          "Station",
            "display_name":     "Name",
            "em_label":         "EM",
            "availability_pct": "Availability %",
            "productive_min":   "Productive (min)",
            "standby_min":      "Standby (min)",
            "down_min":         "Faulted (min)",
            "manual_min":       "Manual (min)",
        })
        summary_section = dash_table.DataTable(
            data=disp.to_dict("records"),
            columns=[{"name": c, "id": c} for c in disp.columns],
            sort_action="native",
            style_table=DT_STYLE_TABLE,
            style_cell=DT_STYLE_CELL,
            style_header=DT_STYLE_HEADER,
            style_data_conditional=AVAIL_PCT_COND,
        )

    # ── Gantt timeline ────────────────────────────────────────────────────────
    if timeline_df.empty:
        gantt_section = html.Div("No timeline data yet.", className="text-muted")
    else:
        # Build y-axis label map: em_id → "Display Name / label"
        labels = (
            timeline_df[["em_id", "display_name", "em_label"]]
            .drop_duplicates("em_id")
            .sort_values("display_name")
        )
        y_map = {
            row["em_id"]: f"{row['display_name']} / {row['em_label']}"
            for _, row in labels.iterrows()
        }

        # Collect segments per state — 4 traces instead of one per row
        buckets: dict[str, dict] = {
            s: {"x": [], "y": [], "base": [], "hover": []} for s in STATE_ORDER
        }

        for _, row in timeline_df.iterrows():
            state = row["state"]
            y_lbl = y_map.get(row["em_id"], str(row["em_id"]))
            t0 = row["ts"]      if pd.notna(row["ts"])      else start
            t1 = row["next_ts"] if pd.notna(row["next_ts"]) else end
            dur_ms = (t1 - t0).total_seconds() * 1000
            b = buckets.get(state, buckets["manual"])
            b["x"].append(dur_ms)
            b["y"].append(y_lbl)
            b["base"].append(t0.timestamp() * 1000)
            b["hover"].append(
                f"{STATE_LABEL.get(state, state)}<br>"
                f"{t0.strftime('%H:%M:%S')} – {t1.strftime('%H:%M:%S')}"
            )

        fig = go.Figure()
        for state in STATE_ORDER:
            d = buckets[state]
            if d["x"]:
                fig.add_trace(go.Bar(
                    x=d["x"],
                    y=d["y"],
                    orientation="h",
                    base=d["base"],
                    marker_color=STATE_COLOR[state],
                    name=STATE_LABEL[state],
                    showlegend=True,
                    customdata=d["hover"],
                    hovertemplate="%{customdata}<extra></extra>",
                    width=0.6,
                ))
            else:
                # Empty trace — keeps legend entry visible for all 4 states
                fig.add_trace(go.Bar(
                    x=[None], y=[None],
                    orientation="h",
                    marker_color=STATE_COLOR[state],
                    name=STATE_LABEL[state],
                    showlegend=True,
                ))

        n_rows = len(labels)
        fig.update_layout(
            title="Availability Timeline — SEMI E10",
            barmode="overlay",
            xaxis=dict(
                type="date",
                range=[start, end],
                title=None,
            ),
            yaxis=dict(autorange="reversed"),
            legend=dict(orientation="h", y=1.1, font_size=11),
            margin=dict(l=0, r=10, t=70, b=20),
            height=max(280, n_rows * 44 + 100),
        )
        gantt_section = dcc.Graph(figure=fig, config={"displayModeBar": False})

    return html.Div([
        html.H6("Availability Summary — SEMI E10", className="mb-2"),
        summary_section,
        html.H6("Timeline", className="mt-4 mb-2"),
        gantt_section,
    ])
