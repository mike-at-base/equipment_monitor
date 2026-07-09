from __future__ import annotations

import datetime
import os
from zoneinfo import ZoneInfo

import pandas as pd
from dash import dash_table, html

import db.queries as q
from app.brand import DT_STYLE_CELL, DT_STYLE_FILTER, DT_STYLE_HEADER, DT_STYLE_TABLE


def _app_tz() -> datetime.tzinfo:
    tz_name = os.environ.get("APP_TIMEZONE", "America/Chicago")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return datetime.timezone.utc


def _shift_window_utc() -> tuple[datetime.datetime, datetime.datetime]:
    tz = _app_tz()
    now_local = datetime.datetime.now(tz)
    start_local = now_local.replace(hour=7, minute=0, second=0, microsecond=0)
    end_local = now_local.replace(hour=15, minute=30, second=0, microsecond=0)
    end_local = min(end_local, now_local)
    if end_local < start_local:
        end_local = start_local
    return start_local.astimezone(datetime.timezone.utc), end_local.astimezone(datetime.timezone.utc)


def render(plc_name: str) -> html.Div:
    plc = plc_name or ""
    if not plc:
        return html.Div("No PLC selected.", className="text-muted")
    ems = q.get_enabled_ems(plc)
    em_ids = [int(e["id"]) for e in ems]
    if not em_ids:
        return html.Div("No enabled stations for selected PLC.", className="text-muted")

    start, end = _shift_window_utc()
    summary = q.query_state_summary(em_ids, start, end)
    flow = q.query_flow_events(em_ids, start, end, limit=10000)
    faults = q.query_fault_pareto_detailed(em_ids, start, end)

    station = summary.groupby("station", as_index=False).agg(
        availability_pct=("availability_pct", "mean"),
        down_min=("down_min", "sum"),
    ) if not summary.empty else pd.DataFrame(columns=["station", "availability_pct", "down_min"])

    if not flow.empty:
        f = flow.copy()
        f["start_ts"] = pd.to_datetime(f["start_ts"], utc=True, errors="coerce")
        f["end_ts"] = pd.to_datetime(f["end_ts"], utc=True, errors="coerce")
        end_ts = pd.Timestamp(end)
        f["end_eff"] = f["end_ts"].fillna(end_ts)
        f["dur_min"] = (f["end_eff"] - f["start_ts"]).dt.total_seconds() / 60.0
        f["dur_min"] = pd.to_numeric(f["dur_min"], errors="coerce").fillna(0.0).clip(lower=0.0)
        flow_station = f.pivot_table(index="station", columns="kind", values="dur_min", aggfunc="sum", fill_value=0.0).reset_index()
        if "blocked" not in flow_station.columns:
            flow_station["blocked"] = 0.0
        if "starved" not in flow_station.columns:
            flow_station["starved"] = 0.0
    else:
        flow_station = pd.DataFrame(columns=["station", "blocked", "starved"])

    merged = station.merge(flow_station, on="station", how="left")
    for c in ("blocked", "starved"):
        merged[c] = pd.to_numeric(merged.get(c), errors="coerce").fillna(0.0)
    merged["flow_loss_min"] = merged["blocked"] + merged["starved"]
    merged = merged.sort_values(["flow_loss_min", "down_min"], ascending=[False, False]).reset_index(drop=True)

    top_issues = merged.head(10).copy()
    top_issues["Availability %"] = top_issues["availability_pct"].map(lambda v: "—" if pd.isna(v) else f"{float(v):.1f}")
    top_issues["Down (min)"] = pd.to_numeric(top_issues["down_min"], errors="coerce").fillna(0.0).map(lambda v: f"{float(v):.1f}")
    top_issues["Blocked (min)"] = top_issues["blocked"].map(lambda v: f"{float(v):.1f}")
    top_issues["Starved (min)"] = top_issues["starved"].map(lambda v: f"{float(v):.1f}")
    top_issues["Flow Loss (min)"] = top_issues["flow_loss_min"].map(lambda v: f"{float(v):.1f}")
    issue_cols = ["station", "Availability %", "Down (min)", "Blocked (min)", "Starved (min)", "Flow Loss (min)"]

    top_faults = pd.DataFrame(columns=["station", "step_name", "fault_count", "total_downtime_min"])
    if not faults.empty:
        ff = faults.copy()
        ff["total_duration_ms"] = pd.to_numeric(ff["total_duration_ms"], errors="coerce").fillna(0.0)
        top_faults = (
            ff.groupby(["station", "step_name"], as_index=False)
            .agg(
                fault_count=("fault_count", "sum"),
                total_ms=("total_duration_ms", "sum"),
            )
            .sort_values(["total_ms", "fault_count"], ascending=[False, False])
            .head(10)
        )
        top_faults["total_downtime_min"] = (top_faults["total_ms"] / 60000.0).map(lambda v: f"{float(v):.1f}")
        top_faults = top_faults[["station", "step_name", "fault_count", "total_downtime_min"]]

    api_note = f"/api/shift_summary?plc={plc}&start={start.isoformat()}&end={end.isoformat()}"
    return html.Div([
        html.H6("AI Analysis Feed", className="mb-2"),
        html.Div("Use this endpoint for agent summaries:", className="text-muted small"),
        html.Code(api_note),
        html.H6("Top Issues This Shift", className="mt-4 mb-2"),
        dash_table.DataTable(
            data=top_issues[issue_cols].to_dict("records"),
            columns=[{"name": c, "id": c} for c in issue_cols],
            sort_action="native",
            style_table=DT_STYLE_TABLE,
            style_cell=DT_STYLE_CELL,
            style_header=DT_STYLE_HEADER,
            style_filter=DT_STYLE_FILTER,
            page_size=10,
        ),
        html.H6("Top Faults This Shift", className="mt-4 mb-2"),
        dash_table.DataTable(
            data=top_faults.to_dict("records"),
            columns=[{"name": c, "id": c} for c in top_faults.columns],
            sort_action="native",
            style_table=DT_STYLE_TABLE,
            style_cell=DT_STYLE_CELL,
            style_header=DT_STYLE_HEADER,
            style_filter=DT_STYLE_FILTER,
            page_size=10,
        ),
    ])
