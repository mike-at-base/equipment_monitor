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
import os
from collections import defaultdict
from zoneinfo import ZoneInfo

import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
from dash import dash_table, dcc, html

import db.queries as q
from app.brand import (
    AVAIL_PCT_COND, BORDER, CONDUIT, ELEVATED, LIVEWIRE, MUTED,
    DT_STYLE_CELL, DT_STYLE_FILTER, DT_STYLE_HEADER, DT_STYLE_TABLE,
    TIME_FMT_TABLE, TIME_FMT_SHORT,
)

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


def _plant_tz() -> datetime.tzinfo:
    tz_name = os.environ.get("APP_TIMEZONE", "America/Chicago")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return datetime.timezone.utc


def _to_plant_time(ts, tz: datetime.tzinfo):
    if ts is None or pd.isna(ts):
        return ts
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(tz)


def render(em_ids: list[int], start: datetime.datetime, end: datetime.datetime) -> html.Div:
    if not em_ids:
        return html.Div("Select at least one station.", className="text-muted p-3")

    summary_df  = q.query_state_summary(em_ids, start, end)
    timeline_df = q.query_state_timeline(em_ids, start, end)
    plant_tz = _plant_tz()
    start_local = _to_plant_time(start, plant_tz)
    end_local = _to_plant_time(end, plant_tz)

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
            t0_local = _to_plant_time(t0, plant_tz)
            t1_local = _to_plant_time(t1, plant_tz)
            dur_ms = (t1 - t0).total_seconds() * 1000
            b = buckets.get(state, buckets["manual"])
            b["x"].append(dur_ms)
            b["y"].append(y_lbl)
            b["base"].append(t0.timestamp() * 1000)
            b["hover"].append(
                f"{STATE_LABEL.get(state, state)}<br>"
                f"{t0_local.strftime(TIME_FMT_SHORT)} – {t1_local.strftime(TIME_FMT_SHORT)}"
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
                range=[start_local, end_local],
                title=None,
            ),
            yaxis=dict(autorange="reversed"),
            legend=dict(orientation="h", y=1.1, font_size=11),
            margin=dict(l=0, r=10, t=70, b=20),
            height=max(280, n_rows * 44 + 100),
        )
        gantt_section = dcc.Graph(figure=fig, config={"displayModeBar": False})

    # ── Down events / fault reasons ───────────────────────────────────────────
    down_section = _render_down_events(em_ids, start, end)

    return html.Div([
        html.H6("Availability Summary — SEMI E10", className="mb-2"),
        _build_state_legend(),
        summary_section,
        html.H6("Timeline", className="mt-4 mb-2"),
        gantt_section,
        html.H6("Down Events & Fault Reasons", className="mt-4 mb-2"),
        down_section,
    ])


# ── State definitions reference card ──────────────────────────────────────────
# Always-visible explanation of the four SEMI E10 states and the availability
# formula.  This lives next to the summary table so anyone opening the modal
# can see how each state is derived from the raw PLC signals (automatic,
# running, fault) without having to ask.

def _state_chip(name: str, color: str, signal_logic: str) -> html.Div:
    """One row: colour swatch + state name + the raw-signal logic that defines it."""
    return html.Div([
        html.Span(style={
            "display":         "inline-block",
            "width":           "10px",
            "height":          "10px",
            "borderRadius":    "9999px",
            "backgroundColor": color,
            "marginRight":     "8px",
            "verticalAlign":   "middle",
        }),
        html.Strong(name, className="me-2"),
        html.Span(signal_logic, className="text-muted small"),
    ], className="mb-1")


def _build_state_legend() -> dbc.Card:
    return dbc.Card(
        dbc.CardBody(
            [
                dbc.Row([
                    dbc.Col([
                        _state_chip(
                            "Productive",
                            STATE_COLOR["productive"],
                            "automatic, running, no fault",
                        ),
                        _state_chip(
                            "Standby",
                            STATE_COLOR["standby"],
                            "automatic, not running, no fault",
                        ),
                    ], md=6),
                    dbc.Col([
                        _state_chip(
                            "Faulted (down)",
                            STATE_COLOR["unscheduled_down"],
                            "fault active in any mode",
                        ),
                        _state_chip(
                            "Manual / Off",
                            STATE_COLOR["manual"],
                            "not in automatic, no fault — excluded from %",
                        ),
                    ], md=6),
                ]),
                html.Hr(className="my-2", style={"borderColor": BORDER}),
                html.Div(
                    [
                        html.Strong("Availability % = "),
                        "(Productive + Standby) ÷ "
                        "(Productive + Standby + Faulted) × 100",
                    ],
                    className="small mb-1",
                ),
                html.Div(
                    [
                        "Manual time is treated as ",
                        html.Em("Non-Scheduled Time"),
                        " under SEMI E10 and is excluded from both the "
                        "numerator and denominator.",
                    ],
                    className="text-muted small",
                ),
            ],
            className="py-2 px-3",
        ),
        className="mb-3",
        style={"backgroundColor": ELEVATED, "borderColor": BORDER},
    )


# ── Down events table ─────────────────────────────────────────────────────────

_REASON_BADGE_COLOR = {
    "step_fault": "#c51808",   # Red  — faulted step
    "interlock":  "#f7c33c",   # Goldenrod — safety interlock
    "manual":     "#3a3733",   # Charcoal — operator manual
    "unknown":    "#9a9794",   # Muted
}

_REASON_LABEL = {
    "step_fault": "Step fault",
    "interlock":  "Interlock",
    "manual":     "Manual",
    "unknown":    "Unknown",
}


def _fmt_duration(ms) -> str:
    if ms is None or pd.isna(ms):
        return "ongoing"
    ms = int(ms)
    if ms < 60_000:
        return f"{ms / 1000:.0f} s"
    if ms < 3_600_000:
        return f"{ms / 60_000:.1f} min"
    return f"{ms / 3_600_000:.1f} h"


def _render_down_events(
    em_ids: list[int],
    start: datetime.datetime,
    end: datetime.datetime,
) -> html.Div:
    df = q.query_down_events(em_ids, start, end)
    plant_tz = _plant_tz()

    if df.empty:
        return html.Div("No down events in selected range.", className="text-muted")

    disp = df.copy()
    disp["Start"]    = disp["start_ts"].apply(
        lambda v: (
            _to_plant_time(v, plant_tz).strftime(TIME_FMT_TABLE)
            if pd.notna(v) else ""
        )
    )
    disp["End"]      = disp["end_ts"].apply(
        lambda v: (
            _to_plant_time(v, plant_tz).strftime(TIME_FMT_TABLE)
            if pd.notna(v) else "ongoing"
        )
    )
    disp["Duration"] = disp["duration_ms"].apply(_fmt_duration)
    disp["Station"]  = disp["station"]
    disp["EM"]       = disp["em_label"]
    disp["Type"]     = disp["reason_type"].map(_REASON_LABEL).fillna(disp["reason_type"])
    disp["Step"]     = disp["step_name"].fillna("")
    # Primary reason: use reason_desc; fall back to fault_msg if desc is blank
    disp["Reason"]   = disp.apply(
        lambda r: r["reason_desc"] or r["fault_msg"] or "",
        axis=1,
    )

    cols_display = ["Start", "End", "Duration", "Station", "EM", "Type", "Step", "Reason"]
    disp = disp[cols_display]

    # Colour-code rows by reason type
    type_cond = []
    for rt, colour in _REASON_BADGE_COLOR.items():
        # Lighten the background to ~25 % opacity for table rows
        r, g, b = (
            int(colour[1:3], 16),
            int(colour[3:5], 16),
            int(colour[5:7], 16),
        )
        type_cond.append({
            "if": {"filter_query": f'{{Type}} eq "{_REASON_LABEL.get(rt, rt)}"'},
            "backgroundColor": f"rgba({r},{g},{b},0.18)",
        })

    # Ongoing rows get a brighter accent on the End cell
    ongoing_cond = [
        {
            "if": {
                "filter_query": '{End} contains "ongoing"',
                "column_id": "End",
            },
            "color": LIVEWIRE,
            "fontWeight": "600",
        }
    ]

    table = dash_table.DataTable(
        data=disp.to_dict("records"),
        columns=[{"name": c, "id": c} for c in cols_display],
        page_size=20,
        sort_action="native",
        filter_action="native",
        style_table=DT_STYLE_TABLE,
        style_cell={**DT_STYLE_CELL, "maxWidth": "340px", "whiteSpace": "normal",
                    "wordBreak": "break-word"},
        style_cell_conditional=[
            {"if": {"column_id": "Reason"}, "minWidth": "240px", "maxWidth": "420px"},
            {"if": {"column_id": "Duration"}, "width": "80px"},
            {"if": {"column_id": "Type"}, "width": "90px"},
        ],
        style_header=DT_STYLE_HEADER,
        style_filter=DT_STYLE_FILTER,
        style_data_conditional=type_cond + ongoing_cond,
    )

    return html.Div([
        html.Small(f"{len(df):,} events", className="text-muted d-block mb-2"),
        table,
    ])
