"""
PLC-wide availability overview panel for the live dashboard.

Shows, on one screen:
  - Lowest availability stations
  - Top unavailability reasons
  - Top faults for the lowest-availability stations

Each row (and chart point) is clickable via DataTable active_cell / clickData so
the main app callback can open the station modal for drill-down.
"""
from __future__ import annotations

import datetime

import pandas as pd
import plotly.graph_objects as go
from dash import dash_table, dcc, html

import db.queries as q
from app.brand import (
    AVAIL_PCT_COND, DT_STYLE_CELL, DT_STYLE_FILTER, DT_STYLE_HEADER, DT_STYLE_TABLE,
)


def _fmt_pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{float(v):.1f}%"


def _fmt_min(v: float | None) -> str:
    if v is None or pd.isna(v):
        return "0.0"
    return f"{float(v):.1f}"


def _station_agg(summary_df: pd.DataFrame) -> pd.DataFrame:
    if summary_df.empty:
        return pd.DataFrame()

    s = summary_df.copy()
    for col in ("productive_min", "standby_min", "down_min", "manual_min"):
        s[col] = pd.to_numeric(s[col], errors="coerce").fillna(0.0)

    g = (
        s.groupby("station", as_index=False)
        .agg(
            display_name=("display_name", "first"),
            productive_min=("productive_min", "sum"),
            standby_min=("standby_min", "sum"),
            down_min=("down_min", "sum"),
            manual_min=("manual_min", "sum"),
        )
    )

    sched = g["productive_min"] + g["standby_min"] + g["down_min"]
    g["availability_pct"] = ((g["productive_min"] + g["standby_min"]) / sched * 100.0).where(sched > 0)
    return g.sort_values(["availability_pct", "down_min"], ascending=[True, False]).reset_index(drop=True)


def _build_lowest_availability(station_df: pd.DataFrame, limit: int = 12) -> tuple[html.Div, pd.DataFrame]:
    if station_df.empty:
        return html.Div("No availability data yet.", className="text-muted"), pd.DataFrame()

    low = station_df.head(limit).copy()
    low["Availability %"] = low["availability_pct"].apply(_fmt_pct)
    low["Faulted (min)"] = low["down_min"].apply(_fmt_min)
    low["Standby (min)"] = low["standby_min"].apply(_fmt_min)
    low["Productive (min)"] = low["productive_min"].apply(_fmt_min)
    low["Manual (min)"] = low["manual_min"].apply(_fmt_min)
    low["Station"] = low["station"]
    low["Name"] = low["display_name"]

    disp = low[[
        "Station", "Name", "Availability %",
        "Faulted (min)", "Standby (min)", "Productive (min)", "Manual (min)",
    ]]

    table = dash_table.DataTable(
        id="avail-lowest-table",
        data=disp.to_dict("records"),
        columns=[{"name": c, "id": c} for c in disp.columns],
        sort_action="native",
        page_size=12,
        style_table=DT_STYLE_TABLE,
        style_cell=DT_STYLE_CELL,
        style_header=DT_STYLE_HEADER,
        style_filter=DT_STYLE_FILTER,
        style_data_conditional=AVAIL_PCT_COND,
    )
    return table, low


def _build_lowest_chart(low_df: pd.DataFrame) -> html.Div:
    if low_df.empty:
        return html.Div("No chart data yet.", className="text-muted")

    fig = go.Figure(
        go.Bar(
            x=low_df["availability_pct"],
            y=low_df["Station"],
            orientation="h",
            customdata=low_df[["Station", "Name"]].to_numpy(),
            marker_color="#c51808",
            hovertemplate="%{customdata[0]} — %{customdata[1]}<br>Availability %{x:.1f}%<extra></extra>",
        )
    )
    fig.update_layout(
        title="Lowest Availability Stations",
        xaxis_title="Availability %",
        yaxis_title=None,
        yaxis={"autorange": "reversed"},
        margin=dict(l=0, r=10, t=40, b=30),
        height=max(280, 26 * len(low_df) + 100),
    )
    return dcc.Graph(id="avail-overview-chart", figure=fig, config={"displayModeBar": False})


def _build_reason_table(down_df: pd.DataFrame, limit: int = 15) -> html.Div:
    if down_df.empty:
        return html.Div("No down-event reasons yet.", className="text-muted")

    d = down_df.copy()
    d["reason"] = d["reason_desc"].fillna("").astype(str).str.strip()
    d["fault_msg"] = d["fault_msg"].fillna("").astype(str).str.strip()
    d["reason"] = d.apply(
        lambda r: r["reason"] if r["reason"] else (r["fault_msg"] if r["fault_msg"] else "(no reason text)"),
        axis=1,
    )
    d["duration_ms"] = pd.to_numeric(d["duration_ms"], errors="coerce").fillna(0.0)

    grp = (
        d.groupby(["station", "reason_type", "reason"], as_index=False)
        .agg(events=("reason", "count"), total_ms=("duration_ms", "sum"))
        .sort_values(["total_ms", "events"], ascending=[False, False])
        .head(limit)
    )
    grp["Total Down (min)"] = (grp["total_ms"] / 60000.0).map(_fmt_min)
    grp["Reason Type"] = grp["reason_type"].fillna("unknown")
    grp["Station"] = grp["station"]
    grp["Reason"] = grp["reason"]
    grp["Events"] = grp["events"].astype(int)
    disp = grp[["Station", "Reason Type", "Reason", "Events", "Total Down (min)"]]

    return dash_table.DataTable(
        id="reason-top-table",
        data=disp.to_dict("records"),
        columns=[{"name": c, "id": c} for c in disp.columns],
        sort_action="native",
        page_size=15,
        style_table=DT_STYLE_TABLE,
        style_cell={**DT_STYLE_CELL, "whiteSpace": "normal", "height": "auto"},
        style_cell_conditional=[
            {"if": {"column_id": "Reason"}, "minWidth": "260px", "maxWidth": "560px"},
        ],
        style_header=DT_STYLE_HEADER,
        style_filter=DT_STYLE_FILTER,
    )


def _build_fault_table(fault_df: pd.DataFrame, lowest_stations: list[str], limit: int = 20) -> html.Div:
    if fault_df.empty or not lowest_stations:
        return html.Div("No fault data for lowest-availability stations.", className="text-muted")

    f = fault_df.copy()
    f = f[f["station"].isin(lowest_stations)]
    if f.empty:
        return html.Div("No fault data for lowest-availability stations.", className="text-muted")

    f["duration_ms"] = pd.to_numeric(f["duration_ms"], errors="coerce").fillna(0.0)
    f["step_name"] = f["step_name"].fillna("").astype(str)
    f["step_desc"] = f["step_desc"].fillna("").astype(str)
    f["ext_fault_msg"] = f["ext_fault_msg"].fillna("").astype(str)
    f["fault"] = f.apply(
        lambda r: (
            f"{r['step_name']} — {r['step_desc']}" if r["step_desc"]
            else (f"{r['step_name']} — {r['ext_fault_msg']}" if r["ext_fault_msg"] else r["step_name"])
        ),
        axis=1,
    )

    grp = (
        f.groupby(["station", "fault"], as_index=False)
        .agg(events=("fault", "count"), total_ms=("duration_ms", "sum"))
        .sort_values(["total_ms", "events"], ascending=[False, False])
        .head(limit)
    )
    grp["Total Fault (min)"] = (grp["total_ms"] / 60000.0).map(_fmt_min)
    grp["Station"] = grp["station"]
    grp["Fault"] = grp["fault"]
    grp["Events"] = grp["events"].astype(int)
    disp = grp[["Station", "Fault", "Events", "Total Fault (min)"]]

    return dash_table.DataTable(
        id="fault-top-table",
        data=disp.to_dict("records"),
        columns=[{"name": c, "id": c} for c in disp.columns],
        sort_action="native",
        page_size=20,
        style_table=DT_STYLE_TABLE,
        style_cell={**DT_STYLE_CELL, "whiteSpace": "normal", "height": "auto"},
        style_cell_conditional=[
            {"if": {"column_id": "Fault"}, "minWidth": "260px", "maxWidth": "560px"},
        ],
        style_header=DT_STYLE_HEADER,
        style_filter=DT_STYLE_FILTER,
    )


def render(plc_name: str, start: datetime.datetime, end: datetime.datetime) -> html.Div:
    rows = q.query_station_status(plc_name)
    if not rows:
        return html.Div("No station data for selected PLC.", className="text-muted")

    em_ids = sorted({int(r["em_id"]) for r in rows if r.get("em_id") is not None})
    if not em_ids:
        return html.Div("No enabled equipment modules for selected PLC.", className="text-muted")

    summary_df = q.query_state_summary(em_ids, start, end)
    down_df = q.query_down_events(em_ids, start, end, limit=5000)
    fault_df = q.query_fault_events(em_ids, None, start, end)

    station_df = _station_agg(summary_df)
    lowest_table, lowest_df = _build_lowest_availability(station_df)
    lowest_chart = _build_lowest_chart(lowest_df)
    reason_table = _build_reason_table(down_df)
    lowest_stations = list(lowest_df["station"].head(6)) if not lowest_df.empty else []
    fault_table = _build_fault_table(fault_df, lowest_stations)

    return html.Div([
        html.Div(
            f"Window: {start.strftime('%Y-%m-%d %H:%M')} to {end.strftime('%Y-%m-%d %H:%M')} UTC",
            className="text-muted small mb-2",
        ),
        html.Div("Click a row or bar to drill into the station details.", className="text-muted small mb-3"),
        html.H6("Lowest Availability Stations", className="mb-2"),
        lowest_table,
        lowest_chart,
        html.H6("Top Unavailability Reasons", className="mt-4 mb-2"),
        reason_table,
        html.H6("Top Faults (Lowest-Availability Stations)", className="mt-4 mb-2"),
        fault_table,
    ])
