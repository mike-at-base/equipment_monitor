"""
Dash app entry point.
    python app/main.py
Opens at http://localhost:8050
"""
from __future__ import annotations

import datetime
import os
import sys
from zoneinfo import ZoneInfo

import dash
import dash_bootstrap_components as dbc
import pandas as pd
from dash import ALL, MATCH, Input, Output, State, callback, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app.brand  # registers Plotly template + exports DT styles
import db.queries as q
from app.layout import build_layout
from app.pages import (
    availability,
    availability_overview,
    configuration,
    cycle_time,
    fault_analysis,
    station_status,
    step_history,
)

# ── App initialisation ────────────────────────────────────────────────────────

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    suppress_callback_exceptions=True,
    title="Equipment Monitor",
)


def _plc_names() -> list[str]:
    try:
        return [p["name"] for p in q.get_all_plcs() if p.get("enabled", True)]
    except Exception:
        return []


app.layout = build_layout(_plc_names() or ["CELL1"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _app_tz() -> datetime.tzinfo:
    tz_name = os.environ.get("APP_TIMEZONE", "America/Chicago")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return datetime.timezone.utc


def _fmt_dt_local_value(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _parse_window(
    start_value, end_value
) -> tuple[datetime.datetime, datetime.datetime]:
    local_tz = _app_tz()

    def _as_local_dt(value, end_bound: bool) -> datetime.datetime:
        if isinstance(value, datetime.datetime):
            dt = value
        elif isinstance(value, datetime.date):
            dt = datetime.datetime.combine(
                value, datetime.time.max if end_bound else datetime.time.min
            )
        elif isinstance(value, str):
            raw = value.strip()
            if not raw:
                raise ValueError("empty datetime value")
            if "T" in raw:
                dt = datetime.datetime.fromisoformat(raw)
            else:
                d = datetime.date.fromisoformat(raw[:10])
                dt = datetime.datetime.combine(
                    d, datetime.time.max if end_bound else datetime.time.min
                )
        else:
            raise ValueError("unsupported datetime value")

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=local_tz)
        return dt

    start_local = _as_local_dt(start_value, end_bound=False)
    end_local = _as_local_dt(end_value, end_bound=True)
    if end_local < start_local:
        end_local = start_local
    start = start_local.astimezone(datetime.timezone.utc)
    end = end_local.astimezone(datetime.timezone.utc)
    return start, end


def _get_em_ids_for_station(plc_name: str, station: str) -> list[int]:
    """All enabled em_ids for a given PLC + station, main EM first."""
    try:
        with q.Conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT e.id
                FROM config_em e
                JOIN config_plc p ON p.id = e.plc_id
                WHERE p.name = %s AND e.station = %s AND e.enabled = TRUE
                ORDER BY CASE WHEN e.em_label = 'main' THEN 0 ELSE 1 END,
                         e.em_label
                """,
                (plc_name, station),
            )
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def _get_station_display_name(plc_name: str, station: str) -> str:
    """Primary display name for a station (main EM label)."""
    try:
        with q.Conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT e.display_name
                FROM config_em e
                JOIN config_plc p ON p.id = e.plc_id
                WHERE p.name = %s AND e.station = %s AND e.em_label = 'main'
                LIMIT 1
                """,
                (plc_name, station),
            )
            row = cur.fetchone()
            return row[0] if row else station
    except Exception:
        return station


# ── Callbacks ─────────────────────────────────────────────────────────────────

@callback(
    Output("live-grid-content", "children"),
    Input("status-interval", "n_intervals"),
    Input("live-interval",   "n_intervals"),
    Input("plc-select",      "value"),
    Input("refresh-btn",     "n_clicks"),
)
def update_live_grid(_si, _li, plc_name, _rb):
    return station_status.render(plc_name or "")


@callback(
    Output("availability-overview-content", "children"),
    Input("status-interval", "n_intervals"),
    Input("live-interval", "n_intervals"),
    Input("plc-select", "value"),
    Input("avail-overview-hours", "value"),
    Input("refresh-btn", "n_clicks"),
)
def update_availability_overview(_si, _li, plc_name, window_hours, _rb):
    plc = plc_name or ""
    if not plc:
        return html.Div("No PLC selected.", className="text-muted p-3")
    hours = int(window_hours or 24)
    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(hours=hours)
    return availability_overview.render(plc, start, end)


@callback(
    Output("station-modal",      "is_open"),
    Output("modal-station-data", "data"),
    Output("modal-tabs",         "active_tab"),
    Input({"type": "station-card", "index": ALL}, "n_clicks"),
    Input("avail-lowest-table", "active_cell"),
    Input("reason-top-table", "active_cell"),
    Input("fault-top-table", "active_cell"),
    Input("avail-overview-chart", "clickData"),
    State("avail-lowest-table", "derived_virtual_data"),
    State("avail-lowest-table", "data"),
    State("reason-top-table", "derived_virtual_data"),
    State("reason-top-table", "data"),
    State("fault-top-table", "derived_virtual_data"),
    State("fault-top-table", "data"),
    State("plc-select", "value"),
    prevent_initial_call=True,
)
def open_station_modal(
    n_clicks_list,
    low_active,
    reason_active,
    fault_active,
    chart_click,
    low_virtual,
    low_data,
    reason_virtual,
    reason_data,
    fault_virtual,
    fault_data,
    plc_name,
):
    """Open the detail modal when any station card is clicked."""
    triggered = ctx.triggered_id
    station = None
    tab = "step-history"

    if isinstance(triggered, dict) and triggered.get("type") == "station-card":
        if any(n_clicks_list):
            station = triggered.get("index")
            tab = "step-history"
    elif triggered == "avail-lowest-table" and low_active:
        rows = low_virtual if low_virtual is not None else (low_data or [])
        idx = low_active.get("row")
        if idx is not None and 0 <= idx < len(rows):
            station = rows[idx].get("Station")
            tab = "availability"
    elif triggered == "reason-top-table" and reason_active:
        rows = reason_virtual if reason_virtual is not None else (reason_data or [])
        idx = reason_active.get("row")
        if idx is not None and 0 <= idx < len(rows):
            station = rows[idx].get("Station")
            tab = "availability"
    elif triggered == "fault-top-table" and fault_active:
        rows = fault_virtual if fault_virtual is not None else (fault_data or [])
        idx = fault_active.get("row")
        if idx is not None and 0 <= idx < len(rows):
            station = rows[idx].get("Station")
            tab = "faults"
    elif triggered == "avail-overview-chart" and chart_click:
        try:
            station = chart_click["points"][0]["customdata"][0]
            tab = "availability"
        except Exception:
            station = None

    if not station:
        raise PreventUpdate

    return True, {"station": station, "plc": plc_name or ""}, tab


@callback(
    Output("modal-station-title", "children"),
    Input("modal-station-data", "data"),
)
def update_modal_title(data):
    if not data:
        return ""
    station      = data.get("station", "")
    plc_name     = data.get("plc", "")
    display_name = _get_station_display_name(plc_name, station)
    return [
        html.Span(display_name, className="fw-bold fs-5"),
        html.Span(f"  ·  {station}", className="text-muted ms-2"),
    ]


@callback(
    Output("modal-tab-content", "children"),
    Input("modal-tabs",         "active_tab"),
    Input("modal-start-dt",     "value"),
    Input("modal-end-dt",       "value"),
    Input("modal-station-data", "data"),
)
def render_modal_tab(active_tab, start_value, end_value, data):
    if not data:
        raise PreventUpdate

    station  = data.get("station", "")
    plc_name = data.get("plc", "")
    em_ids   = _get_em_ids_for_station(plc_name, station)

    if not em_ids:
        return dbc.Alert(
            "No equipment modules found for this station.", color="warning"
        )

    now_local = datetime.datetime.now(_app_tz())
    default_start = _fmt_dt_local_value(now_local - datetime.timedelta(hours=8))
    default_end = _fmt_dt_local_value(now_local)
    start, end = _parse_window(
        start_value or default_start,
        end_value or default_end,
    )

    if active_tab == "step-history":
        return step_history.render(em_ids, start, end)
    if active_tab == "cycle-time":
        return cycle_time.render(em_ids, start, end)
    if active_tab == "faults":
        return fault_analysis.render(em_ids, start, end)
    if active_tab == "availability":
        return availability.render(em_ids, start, end)

    return html.Div("Unknown tab.")


@callback(
    Output("modal-start-dt", "value"),
    Output("modal-end-dt",   "value"),
    Input("range-1h-btn",  "n_clicks"),
    Input("range-4h-btn",  "n_clicks"),
    Input("range-8h-btn",  "n_clicks"),
    Input("range-24h-btn", "n_clicks"),
    prevent_initial_call=True,
)
def set_quick_range(_n1, _n4, _n8, _n24):
    hours_by_button = {
        "range-1h-btn": 1,
        "range-4h-btn": 4,
        "range-8h-btn": 8,
        "range-24h-btn": 24,
    }
    btn = ctx.triggered_id
    hours = hours_by_button.get(btn)
    if hours is None:
        raise PreventUpdate
    now_local = datetime.datetime.now(_app_tz()).replace(second=0, microsecond=0)
    start_local = now_local - datetime.timedelta(hours=hours)
    return _fmt_dt_local_value(start_local), _fmt_dt_local_value(now_local)


@callback(
    Output("config-modal",      "is_open"),
    Output("config-modal-body", "children"),
    Input("config-btn", "n_clicks"),
    prevent_initial_call=True,
)
def toggle_config_modal(n):
    if n:
        return True, configuration.render()
    raise PreventUpdate


@callback(
    Output("step-dur-chart", "figure"),
    Input("step-dur-slider", "value"),
    Input("step-history-table", "derived_virtual_data"),
    State("step-history-table", "data"),
    prevent_initial_call=True,
)
def update_step_duration_chart(max_seconds, filtered_rows, table_data):
    """
    Recompute the Average Step Duration bar chart whenever the outlier slider
    moves.  ``max_seconds`` is the inclusive cutoff in seconds — samples
    longer than this are excluded before the per-step mean is computed.
    The source rows come from the table's active filtered/sorted view.
    """
    if filtered_rows is None:
        filtered_rows = table_data or []
    if not filtered_rows:
        return step_history.build_step_duration_figure(pd.DataFrame())

    filtered_df = pd.DataFrame(filtered_rows)
    if "Step" not in filtered_df.columns:
        raise PreventUpdate

    # Prefer raw milliseconds from the hidden column. If it's missing (some
    # Dash table update paths can omit hidden fields), fall back to parsing the
    # human-readable "Duration" text.
    if "duration_ms_raw" in filtered_df.columns:
        duration_ms = pd.to_numeric(filtered_df["duration_ms_raw"], errors="coerce")
    elif "duration_ms" in filtered_df.columns:
        duration_ms = pd.to_numeric(filtered_df["duration_ms"], errors="coerce")
    elif "Duration" in filtered_df.columns:
        duration_text = filtered_df["Duration"].astype(str).str.strip()
        nums = pd.to_numeric(
            duration_text.str.extract(r"([0-9]*\.?[0-9]+)", expand=False),
            errors="coerce",
        )
        duration_ms = pd.Series(float("nan"), index=filtered_df.index, dtype="float64")
        ms_mask = duration_text.str.contains(r"\bms\b", case=False, na=False)
        sec_mask = duration_text.str.contains(r"\bs\b", case=False, na=False)
        min_mask = duration_text.str.contains(r"\bmin\b", case=False, na=False)
        duration_ms.loc[ms_mask] = nums.loc[ms_mask]
        duration_ms.loc[sec_mask] = nums.loc[sec_mask] * 1000
        duration_ms.loc[min_mask] = nums.loc[min_mask] * 60_000
    else:
        return step_history.build_step_duration_figure(pd.DataFrame())

    step_desc = (
        filtered_df["Description"]
        if "Description" in filtered_df.columns
        else pd.Series("", index=filtered_df.index)
    )

    prod_df = pd.DataFrame({
        "step_name": filtered_df["Step"],
        "step_desc": step_desc,
        "duration_ms": duration_ms,
    })
    prod_df = prod_df[
        prod_df["duration_ms"].notna()
        & (prod_df["duration_ms"] > 0)
        & (prod_df["step_name"] != "STEP_STOP")
        & (prod_df["step_name"] != "STEP_FINAL")
    ]

    max_ms = (max_seconds or 0) * 1000
    return step_history.build_step_duration_figure(
        prod_df, max_duration_ms=max_ms,
    )


@callback(
    Output("step-history-export-download", "data"),
    Input("step-history-export-btn", "n_clicks"),
    State("modal-station-data", "data"),
    State("modal-start-dt", "value"),
    State("modal-end-dt", "value"),
    prevent_initial_call=True,
)
def export_step_history_csv(n_clicks, station_data, start_value, end_value):
    if not n_clicks or not station_data:
        raise PreventUpdate

    station = station_data.get("station", "")
    plc_name = station_data.get("plc", "")
    em_ids = _get_em_ids_for_station(plc_name, station)
    if not em_ids:
        raise PreventUpdate

    now_local = datetime.datetime.now(_app_tz())
    default_start = _fmt_dt_local_value(now_local - datetime.timedelta(hours=8))
    default_end = _fmt_dt_local_value(now_local)
    start, end = _parse_window(
        start_value or default_start,
        end_value or default_end,
    )

    df = q.query_step_history(em_ids, None, start, end, limit=None)
    if df.empty:
        raise PreventUpdate

    ts_local = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(_app_tz())
    df["timestamp_local"] = (
        ts_local.dt.strftime("%Y-%m-%d %I:%M:%S.%f").str[:-3]
        + " " + ts_local.dt.strftime("%p")
    )
    df = df.rename(columns={
        "ts": "timestamp_utc",
        "station": "station",
        "em_label": "em",
        "seq_name": "sequence",
        "step_name": "step",
        "step_desc": "description",
        "duration_ms": "duration_ms",
        "was_faulted": "was_faulted",
    })
    export_cols = [
        "timestamp_local", "timestamp_utc", "station", "em", "sequence",
        "step", "description", "duration_ms", "was_faulted",
    ]
    df = df[export_cols]

    filename_station = (station or "station").replace(" ", "_")
    filename = f"step_history_{filename_station}.csv"
    return dcc.send_data_frame(df.to_csv, filename, index=False)


@callback(
    Output({"type": "cycle-step-table", "key": MATCH}, "data"),
    Output({"type": "cycle-step-table", "key": MATCH}, "selected_rows"),
    Output({"type": "cycle-step-base", "key": MATCH}, "data"),
    Input({"type": "cycle-table", "key": MATCH}, "active_cell"),
    State({"type": "cycle-table", "key": MATCH}, "derived_virtual_data"),
    State({"type": "cycle-table", "key": MATCH}, "data"),
    prevent_initial_call=True,
)
def load_cycle_step_history(active_cell, filtered_cycles, all_cycles):
    if not active_cell:
        raise PreventUpdate
    rows = filtered_cycles if filtered_cycles is not None else (all_cycles or [])
    row_idx = active_cell.get("row")
    if row_idx is None or row_idx < 0 or row_idx >= len(rows):
        raise PreventUpdate

    cycle = rows[row_idx]
    key = (ctx.triggered_id or {}).get("key", "")
    try:
        em_id_str, seq_idx_str = str(key).split(":")
        em_id = int(em_id_str)
        seq_idx = int(seq_idx_str)
    except Exception:
        raise PreventUpdate

    start_ts = pd.Timestamp(cycle.get("cycle_start_utc")).to_pydatetime()
    end_ts = pd.Timestamp(cycle.get("cycle_end_utc")).to_pydatetime()
    steps = q.query_cycle_steps(em_id, seq_idx, start_ts, end_ts)
    if steps.empty:
        base_ms = float(cycle.get("cycle_ms_raw") or 0.0)
        base = {"cycle_ms_raw": base_ms, "removed_ms": 0.0}
        return [], [], base

    steps["duration_ms"] = pd.to_numeric(steps["duration_ms"], errors="coerce")
    steps["ts_local"] = pd.to_datetime(steps["ts"], utc=True).dt.tz_convert(_app_tz())
    steps["Timestamp"] = (
        steps["ts_local"].dt.strftime("%Y-%m-%d %I:%M:%S.%f").str[:-3]
        + " " + steps["ts_local"].dt.strftime("%p")
    )
    step_rows = pd.DataFrame({
        "Step": steps["step_name"].fillna(""),
        "Description": steps["step_desc"].fillna(""),
        "Timestamp": steps["Timestamp"],
        "Duration": steps["duration_ms"].apply(cycle_time._fmt_ms),
        "Faulted": steps["was_faulted"].map({True: "yes", False: ""}),
        "duration_ms_raw": steps["duration_ms"],
        "cycle_ms_raw": float(cycle.get("cycle_ms_raw") or 0.0),
    })
    base = {"cycle_ms_raw": float(cycle.get("cycle_ms_raw") or 0.0), "removed_ms": 0.0}
    return step_rows.to_dict("records"), [], base


@callback(
    Output({"type": "cycle-step-summary", "key": MATCH}, "children"),
    Input({"type": "cycle-step-table", "key": MATCH}, "selected_rows"),
    Input({"type": "cycle-step-table", "key": MATCH}, "data"),
    Input({"type": "cycle-step-base", "key": MATCH}, "data"),
    prevent_initial_call=True,
)
def update_cycle_step_summary(selected_rows, step_rows, base_data):
    if not step_rows:
        return "Select a cycle row to load step history."
    base_ms = float((base_data or {}).get("cycle_ms_raw") or 0.0)
    selected_rows = selected_rows or []

    removed_ms = 0.0
    removed_steps: list[str] = []
    for i in selected_rows:
        if not isinstance(i, int) or i < 0 or i >= len(step_rows):
            continue
        row = step_rows[i]
        ms = pd.to_numeric(pd.Series([row.get("duration_ms_raw")]), errors="coerce").iloc[0]
        if pd.notna(ms):
            removed_ms += float(ms)
        step_name = str(row.get("Step") or "").strip()
        if step_name:
            removed_steps.append(step_name)

    adjusted_ms = max(0.0, base_ms - removed_ms)
    removed_label = ", ".join(removed_steps[:6])
    if len(removed_steps) > 6:
        removed_label += f" (+{len(removed_steps) - 6} more)"
    if not removed_label:
        removed_label = "none"
    return (
        f"Original cycle: {cycle_time._fmt_ms(base_ms)} | "
        f"Excluded total: {cycle_time._fmt_ms(removed_ms)} ({len(selected_rows)} step(s): {removed_label}) | "
        f"What-if cycle: {cycle_time._fmt_ms(adjusted_ms)}"
    )


@callback(
    Output({"type": "cycle-export-download", "key": MATCH}, "data"),
    Input({"type": "cycle-export-btn", "key": MATCH}, "n_clicks"),
    State({"type": "cycle-table", "key": MATCH}, "derived_virtual_data"),
    State({"type": "cycle-table", "key": MATCH}, "data"),
    prevent_initial_call=True,
)
def export_cycle_rows(_n_clicks, filtered_cycles, all_cycles):
    rows = filtered_cycles if filtered_cycles is not None else (all_cycles or [])
    if not rows:
        raise PreventUpdate
    df = pd.DataFrame(rows).copy()
    if df.empty:
        raise PreventUpdate
    keep_cols = [
        "Cycle Start", "Cycle End", "Cycle Length", "Cycle Length (s)",
        "Station", "Sequence",
    ]
    cols = [c for c in keep_cols if c in df.columns]
    if cols:
        df = df[cols]
    key = (ctx.triggered_id or {}).get("key", "cycles")
    filename = f"cycles_{str(key).replace(':', '_')}.csv"
    return dcc.send_data_frame(df.to_csv, filename, index=False)


@callback(
    Output("yaml-save-status", "children"),
    Input("yaml-save-btn",  "n_clicks"),
    State("yaml-editor",    "value"),
    prevent_initial_call=True,
)
def save_yaml(n_clicks, content):
    if not n_clicks or not content:
        raise PreventUpdate
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config.yaml"
    )
    try:
        import yaml
        yaml.safe_load(content)
        with open(config_path, "w") as f:
            f.write(content)
        return dbc.Alert(
            "✓ Saved. Restart the collector to apply changes.",
            color="success", duration=4000,
        )
    except Exception as e:
        return dbc.Alert(f"Error: {e}", color="danger")


if __name__ == "__main__":
    # DASH_DEBUG=true enables Dash's dev server (hot-reload, verbose errors).
    # Defaults to True so `python app/main.py` for local development still
    # hot-reloads.  docker-compose.yml sets DASH_DEBUG=false so containers
    # never expose the dev server.
    debug = os.environ.get("DASH_DEBUG", "true").lower() in ("true", "1", "yes")
    app.run(debug=debug, host="0.0.0.0", port=8050)
