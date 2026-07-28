"""
Dash app entry point.
    python app/main.py
Opens at http://localhost:8050
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from zoneinfo import ZoneInfo

import dash
import dash_bootstrap_components as dbc
import pandas as pd
from dash import (
    ALL,
    MATCH,
    Input,
    Output,
    State,
    callback,
    clientside_callback,
    ctx,
    dcc,
    html,
    no_update,
)
from dash.exceptions import PreventUpdate
from flask import jsonify, request  # Dash bundles Flask; app.server is the Flask app

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app.brand  # registers Plotly template + exports DT styles
import db.queries as q
from app.layout import NAV_VIEWS, build_layout
from app.pages import (
    analysis_hub,
    availability,
    availability_overview,
    configuration,
    cycle_time,
    daily_digest,
    fault_analysis,
    line_issues_timeline,
    mod_bottleneck,
    runtime_transitions,
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
    Output("dashboard-view", "data"),
    Input("view-live-status-btn", "n_clicks"),
    Input("view-availability-overview-btn", "n_clicks"),
    Input("view-daily-digest-btn", "n_clicks"),
    Input("view-mod-bottleneck-btn", "n_clicks"),
    Input("view-line-issues-btn", "n_clicks"),
    Input("view-analysis-btn", "n_clicks"),
    prevent_initial_call=True,
)
def set_dashboard_view(_live, _avail, _digest, _mod, _line, _analysis):
    trig = ctx.triggered_id
    if trig == "view-availability-overview-btn":
        return "availability-overview"
    if trig == "view-daily-digest-btn":
        return "daily-digest"
    if trig == "view-mod-bottleneck-btn":
        return "mod-bottleneck"
    if trig == "view-line-issues-btn":
        return "line-issues"
    if trig == "view-analysis-btn":
        return "analysis"
    return "live-status"


@callback(
    [Output(btn_id, "className") for btn_id, _lbl in NAV_VIEWS.values()]
    + [Output(f"toolbar-{view}", "className") for view in NAV_VIEWS],
    Input("dashboard-view", "data"),
)
def update_nav_state(view):
    """Highlight the active nav button and show only that view's toolbar."""
    view = view or "live-status"
    btn_classes = [
        "sidebar-nav-btn active" if v == view else "sidebar-nav-btn"
        for v in NAV_VIEWS
    ]
    toolbar_classes = [
        "view-toolbar" if v == view else "view-toolbar d-none"
        for v in NAV_VIEWS
    ]
    return btn_classes + toolbar_classes


def _loading(content_id: str):
    return dcc.Loading(
        html.Div(id=content_id),
        type="circle",
        color="#b2dd79",
    )


@callback(
    Output("dashboard-main-content", "children"),
    Input("plc-select", "value"),
    Input("dashboard-view", "data"),
    Input("refresh-btn", "n_clicks"),
)
def render_view_shell(plc_name, view, _rb):
    """
    Mount the thin shell for the active view.  The shell's content div is
    filled by that view's own callback (fires on mount and whenever one of
    the view's OWN controls changes) — deliberately no other inputs here so
    unrelated control changes can't re-render the page.
    """
    plc = plc_name or ""
    if not plc:
        return html.Div("No PLC selected.", className="text-muted p-3")
    view = view or "live-status"

    if view == "availability-overview":
        return _loading("avail-overview-content")
    if view == "daily-digest":
        return _loading("daily-digest-content")
    if view == "mod-bottleneck":
        return _loading("mod-bottleneck-content")
    if view == "line-issues":
        return _loading("line-issues-content")
    if view == "analysis":
        return analysis_hub.render(plc)
    # live-status: render the grid inline — an intermediate callback firing
    # into a just-mounted div races Dash's layout injection and can leave
    # the page empty when switching back to this view.
    return html.Div(
        station_status.render(plc), id="live-grid-content", className="pt-2",
    )


@callback(
    Output("avail-overview-content", "children"),
    Input("avail-overview-hours", "value"),
    State("plc-select", "value"),
)
def render_availability_overview_content(window_hours, plc_name):
    plc = plc_name or ""
    if not plc:
        raise PreventUpdate
    hours = int(window_hours or 24)
    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(hours=hours)
    return availability_overview.render(plc, start, end)


@callback(
    Output("daily-digest-content", "children"),
    Input("daily-digest-shift-hours", "value"),
    State("plc-select", "value"),
)
def render_daily_digest_content(shift_hours, plc_name):
    plc = plc_name or ""
    if not plc:
        raise PreventUpdate
    shift = int(shift_hours or 8)
    end = datetime.datetime.now(datetime.timezone.utc)
    return daily_digest.render(plc, shift, end)


@callback(
    Output("mod-bottleneck-content", "children"),
    Input("bottleneck-excluded-stations", "value"),
    State("plc-select", "value"),
)
def render_mod_bottleneck_content(excluded, plc_name):
    plc = plc_name or ""
    if not plc:
        raise PreventUpdate
    return mod_bottleneck.render(plc, excluded or [])


@callback(
    Output("line-issues-content", "children"),
    Input("line-issues-plc-filter", "value"),
    Input("line-issues-excluded-stations", "value"),
    Input("line-issues-start-dt", "value"),
    Input("line-issues-end-dt", "value"),
)
def render_line_issues_content(plcs, excluded_stations, start_value, end_value):
    try:
        if start_value and end_value:
            start, end = _parse_window(start_value, end_value)
        else:
            raise ValueError("line timeline datetime not set")
    except Exception:
        end = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(hours=24)
    return line_issues_timeline.render(
        start, end, plcs or [], excluded_stations or [],
    )


@callback(
    Output("bottleneck-excluded-stations", "options"),
    Output("bottleneck-excluded-stations", "value"),
    Input("plc-select", "value"),
    State("bottleneck-excluded-stations", "value"),
)
def update_bottleneck_station_filter(plc_name, selected):
    plc = plc_name or ""
    stations = q.get_stations_for_plc(plc) if plc else []
    opts = []
    station_values: list[str] = []
    for s in stations:
        station = str(s.get("station") or "")
        if not station:
            continue
        label = f"{station} — {s.get('display_name') or station}"
        opts.append({"label": label, "value": station})
        station_values.append(station)
    valid = set(station_values)
    selected_set = {str(v) for v in (selected or []) if str(v) in valid}
    if not selected_set and plc == "MOD1":
        defaults = {"ST36000-01", "ST36000-02"}
        selected_set = defaults & valid
    selected_out = sorted(selected_set)
    return opts, selected_out


@callback(
    Output("line-issues-plc-filter", "options"),
    Output("line-issues-plc-filter", "value"),
    Input("plc-select", "value"),
    State("line-issues-plc-filter", "value"),
)
def update_line_issues_plc_filter(_plc_name, selected):
    plcs = [p["name"] for p in q.get_all_plcs() if p.get("enabled", True)]
    options = [{"label": p, "value": p} for p in plcs]
    valid = set(plcs)
    selected_set = {str(v) for v in (selected or []) if str(v) in valid}
    if not selected_set:
        selected_set = valid
    return options, sorted(selected_set)


@callback(
    Output("line-issues-excluded-stations", "options"),
    Output("line-issues-excluded-stations", "value"),
    Input("line-issues-plc-filter", "value"),
    State("line-issues-excluded-stations", "value"),
)
def update_line_issues_station_filter(selected_plcs, selected_stations):
    station_opts: list[dict] = []
    station_keys: list[str] = []
    for plc in (selected_plcs or []):
        for em in q.get_enabled_ems(plc):
            if str(em.get("em_label") or "").lower() != "main":
                continue
            station = str(em.get("station") or "")
            if not station:
                continue
            key = f"{plc}|{station}"
            label = f"{plc} / {station} — {em.get('display_name') or station}"
            station_opts.append({"label": label, "value": key})
            station_keys.append(key)
    valid = set(station_keys)
    selected_set = {str(v) for v in (selected_stations or []) if str(v) in valid}
    return station_opts, sorted(selected_set)


@callback(
    Output({"type": "em-row-body", "em_id": ALL}, "children"),
    Output("live-conn-banner", "children"),
    Output("live-conn-banner", "className"),
    Input("live-interval", "n_intervals"),
    Input("refresh-btn", "n_clicks"),
    Input("plc-select", "value"),
    Input("dashboard-view", "data"),
    State({"type": "em-row-body", "em_id": ALL}, "id"),
)
def update_live_row_values(_li, _rb, plc_name, view, row_ids):
    if (view or "live-status") != "live-status":
        raise PreventUpdate
    if not row_ids:
        raise PreventUpdate

    plc = plc_name or ""
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        rows = station_status.query_station_status_cached(plc)
    except Exception:
        rows = []

    by_em_id = {
        station_status.em_row_id(row): row
        for row in rows
    }
    row_children = []
    for row_id in row_ids:
        em_key = (row_id or {}).get("em_id")
        em_row = by_em_id.get(em_key)
        if em_row is None:
            row_children.append(no_update)
        else:
            row_children.append(station_status.em_row_children(em_row, now))

    banner_children, banner_class = station_status.connection_banner_props(plc)
    return row_children, banner_children, banner_class


clientside_callback(
    """
    function(_tick, view) {
        if ((view || "live-status") !== "live-status") {
            return window.dash_clientside.no_update;
        }
        if (!window.equipmentMonitorLiveTimerTick) {
            return window.dash_clientside.no_update;
        }
        window.equipmentMonitorLiveTimerTick();
        return window.dash_clientside.no_update;
    }
    """,
    Output("live-timer-noop", "children"),
    Input("status-interval", "n_intervals"),
    Input("dashboard-view", "data"),
)


@callback(
    Output("app-sidebar", "className"),
    Output("app-main", "className"),
    Output("sidebar-toggle-btn", "children"),
    Output("sidebar-collapsed", "data"),
    Input("sidebar-toggle-btn", "n_clicks"),
    State("sidebar-collapsed", "data"),
    prevent_initial_call=True,
)
def toggle_sidebar(_n, collapsed):
    is_collapsed = not bool(collapsed)
    if is_collapsed:
        return "app-sidebar collapsed", "app-main expanded", "☰", True
    return "app-sidebar", "app-main", "◧", False


@callback(
    Output("daily-digest-export-download", "data"),
    Input("daily-digest-export-btn", "n_clicks"),
    State("plc-select", "value"),
    State("daily-digest-shift-hours", "value"),
    prevent_initial_call=True,
)
def export_daily_digest(n_clicks, plc_name, shift_hours):
    if not n_clicks:
        raise PreventUpdate
    plc = plc_name or ""
    if not plc:
        raise PreventUpdate
    hours = int(shift_hours or 8)
    end = datetime.datetime.now(datetime.timezone.utc)
    payload = daily_digest.build_export_payload(plc, hours, end)
    stamp = end.strftime("%Y%m%d_%H%M")
    filename = f"daily_digest_{plc}_{hours}h_{stamp}.json".replace(" ", "_")
    return dict(content=json.dumps(payload, indent=2), filename=filename)


@callback(
    Output("station-modal",      "is_open"),
    Output("modal-station-data", "data"),
    Output("modal-tabs",         "active_tab"),
    Input({"type": "station-card", "index": ALL}, "n_clicks"),
    State("plc-select", "value"),
    prevent_initial_call=True,
)
def open_station_modal_from_card(n_clicks_list, plc_name):
    """Open station modal from Live Status card clicks."""
    triggered = ctx.triggered_id
    if not (isinstance(triggered, dict) and triggered.get("type") == "station-card"):
        raise PreventUpdate
    # Newly-mounted pattern-matching inputs fire this callback with
    # n_clicks 0/None on every grid render — only react to a real click.
    trig_value = ctx.triggered[0].get("value") if ctx.triggered else None
    if not trig_value:
        raise PreventUpdate
    station = triggered.get("index")
    if not station:
        raise PreventUpdate
    return True, {"station": station, "plc": plc_name or ""}, "step-history"


@callback(
    Output("station-modal",      "is_open", allow_duplicate=True),
    Output("modal-station-data", "data", allow_duplicate=True),
    Output("modal-tabs",         "active_tab", allow_duplicate=True),
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
def open_station_modal_from_overview(
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
    """Open station modal from availability overview interactions."""
    triggered = ctx.triggered_id
    station = None
    tab = "availability"

    if triggered == "avail-lowest-table" and low_active:
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
    Output("station-modal",      "is_open", allow_duplicate=True),
    Output("modal-station-data", "data", allow_duplicate=True),
    Output("modal-tabs",         "active_tab", allow_duplicate=True),
    Input("mod-bottleneck-table", "active_cell"),
    Input("mod-bottleneck-chart", "clickData"),
    State("mod-bottleneck-table", "derived_virtual_data"),
    State("mod-bottleneck-table", "data"),
    State("plc-select", "value"),
    prevent_initial_call=True,
)
def open_station_modal_from_mod_bottleneck(
    active_cell,
    chart_click,
    table_virtual,
    table_data,
    plc_name,
):
    triggered = ctx.triggered_id
    station = None
    if triggered == "mod-bottleneck-table" and active_cell:
        rows = table_virtual if table_virtual is not None else (table_data or [])
        idx = active_cell.get("row")
        if idx is not None and 0 <= idx < len(rows):
            station = rows[idx].get("Station")
    elif triggered == "mod-bottleneck-chart" and chart_click:
        try:
            station = chart_click["points"][0]["customdata"][0]
        except Exception:
            station = None
    if not station:
        raise PreventUpdate
    return True, {"station": station, "plc": plc_name or ""}, "runtime-transitions"


@callback(
    Output("station-modal",      "is_open", allow_duplicate=True),
    Output("modal-station-data", "data", allow_duplicate=True),
    Output("modal-tabs",         "active_tab", allow_duplicate=True),
    Input("line-issues-timeline-chart", "clickData"),
    prevent_initial_call=True,
)
def open_station_modal_from_line_timeline(chart_click):
    if not chart_click:
        raise PreventUpdate
    try:
        cd = chart_click["points"][0].get("customdata") or []
        station = cd[0]
        plc_name = cd[1]
    except Exception:
        raise PreventUpdate
    if not station or not plc_name:
        raise PreventUpdate
    return True, {"station": station, "plc": plc_name}, "runtime-transitions"


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
    if active_tab == "runtime-transitions":
        return runtime_transitions.render(em_ids, start, end)

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


# ── Passdown metrics API ──────────────────────────────────────────────────────
# Read-only JSON consumed by the Brain's nightly passdown. Reuses the existing
# SEMI E10 availability summary and fault pareto queries so the math lives in
# exactly one place. Optionally guarded by PASSDOWN_API_KEY (X-API-Key header).

def _num(v) -> float | None:
    """Coerce a value to float, mapping NaN/None to None for clean JSON."""
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _ms_to_min(v) -> float | None:
    n = _num(v)
    return round(n / 60000.0, 1) if n is not None else None


def _str(v) -> str | None:
    """Coerce a value to str, mapping NaN/None to None for valid JSON.

    Pandas yields float NaN for missing string columns (e.g. a fault with no
    matched sequence name). Flask's JSON encoder serializes that as a bare
    ``NaN`` token, which is invalid JSON and breaks strict decoders (the Brain's
    Go client rejects the whole payload). Normalizing to null keeps it valid.
    """
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return str(v)


def _parse_iso_utc(value: str | None) -> datetime.datetime:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("missing timestamp")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _build_shift_summary_payload(
    plc: str, start: datetime.datetime, end: datetime.datetime
) -> dict:
    ems = q.get_enabled_ems(plc)
    em_ids = [int(e["id"]) for e in ems]
    if not em_ids:
        return {
            "plc": plc,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "stations": [],
            "top_faults": [],
            "bottleneck_station": None,
        }

    summary_df = q.query_state_summary(em_ids, start, end)
    flow_df = q.query_flow_events(em_ids, start, end, limit=15000)
    faults_df = q.query_fault_pareto_detailed(em_ids, start, end)

    station = (
        summary_df.groupby("station", as_index=False)
        .agg(
            display_name=("display_name", "first"),
            availability_pct=("availability_pct", "mean"),
            productive_min=("productive_min", "sum"),
            standby_min=("standby_min", "sum"),
            down_min=("down_min", "sum"),
            manual_min=("manual_min", "sum"),
        )
        if not summary_df.empty
        else pd.DataFrame(
            columns=[
                "station", "display_name", "availability_pct",
                "productive_min", "standby_min", "down_min", "manual_min",
            ]
        )
    )

    if not flow_df.empty:
        f = flow_df.copy()
        f["start_ts"] = pd.to_datetime(f["start_ts"], utc=True, errors="coerce")
        f["end_ts"] = pd.to_datetime(f["end_ts"], utc=True, errors="coerce")
        end_ts = pd.Timestamp(end)
        f["end_eff"] = f["end_ts"].fillna(end_ts)
        f["dur_min"] = (f["end_eff"] - f["start_ts"]).dt.total_seconds() / 60.0
        f["dur_min"] = pd.to_numeric(f["dur_min"], errors="coerce").fillna(0.0).clip(lower=0.0)
        flow_station = (
            f.pivot_table(
                index="station",
                columns="kind",
                values="dur_min",
                aggfunc="sum",
                fill_value=0.0,
            )
            .reset_index()
        )
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
    merged = merged.sort_values(
        ["flow_loss_min", "down_min", "availability_pct"],
        ascending=[False, False, True],
    ).reset_index(drop=True)

    top_faults: list[dict] = []
    if not faults_df.empty:
        ff = faults_df.copy()
        ff["total_duration_ms"] = pd.to_numeric(ff["total_duration_ms"], errors="coerce").fillna(0.0)
        top = (
            ff.groupby(["station", "step_name"], as_index=False)
            .agg(
                fault_count=("fault_count", "sum"),
                total_ms=("total_duration_ms", "sum"),
            )
            .sort_values(["total_ms", "fault_count"], ascending=[False, False])
            .head(15)
        )
        top_faults = [
            {
                "station": _str(r.get("station")),
                "step_name": _str(r.get("step_name")),
                "fault_count": int(r.get("fault_count") or 0),
                "total_downtime_min": _ms_to_min(r.get("total_ms")),
            }
            for _, r in top.iterrows()
        ]

    stations = [
        {
            "station": _str(r.get("station")),
            "display_name": _str(r.get("display_name")),
            "availability_pct": _num(r.get("availability_pct")),
            "productive_min": _num(r.get("productive_min")),
            "standby_min": _num(r.get("standby_min")),
            "down_min": _num(r.get("down_min")),
            "manual_min": _num(r.get("manual_min")),
            "blocked_min": _num(r.get("blocked")),
            "starved_min": _num(r.get("starved")),
            "flow_loss_min": _num(r.get("flow_loss_min")),
        }
        for _, r in merged.iterrows()
    ]
    bottleneck_station = stations[0] if stations else None
    return {
        "plc": plc,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "stations": stations,
        "top_faults": top_faults,
        "bottleneck_station": bottleneck_station,
    }


@app.server.route("/api/passdown")
def api_passdown():
    """GET /api/passdown?plc=<name>&start=<iso>&end=<iso>

    Returns per-EM availability (SEMI E10) and a fault pareto for one PLC over
    the window. start/end are ISO 8601; naive values are treated as UTC.
    """
    api_key = os.environ.get("PASSDOWN_API_KEY", "").strip()
    if api_key and request.headers.get("X-API-Key", "").strip() != api_key:
        return jsonify({"error": "unauthorized"}), 401

    plc = (request.args.get("plc") or "").strip()
    if not plc:
        return jsonify({"error": "plc query parameter is required"}), 400
    try:
        start = _parse_iso_utc(request.args.get("start"))
        end = _parse_iso_utc(request.args.get("end"))
    except ValueError as e:
        return jsonify({"error": f"invalid start/end: {e}"}), 400

    ems = q.get_enabled_ems(plc)
    em_ids = [e["id"] for e in ems]
    if not em_ids:
        return jsonify({
            "plc": plc, "start": start.isoformat(), "end": end.isoformat(),
            "availability": [], "pareto": [],
        })

    avail_df = q.query_state_summary(em_ids, start, end)
    pareto_df = q.query_fault_pareto_detailed(em_ids, start, end)

    availability = [
        {
            "station": _str(r.get("station")),
            "display_name": _str(r.get("display_name")),
            "em_label": _str(r.get("em_label")),
            "availability_pct": _num(r.get("availability_pct")),
            "productive_min": _num(r.get("productive_min")),
            "standby_min": _num(r.get("standby_min")),
            "down_min": _num(r.get("down_min")),
            "manual_min": _num(r.get("manual_min")),
        }
        for _, r in avail_df.iterrows()
    ]

    pareto = [
        {
            "station": _str(r.get("station")),
            "display_name": _str(r.get("display_name")),
            "em_label": _str(r.get("em_label")),
            "seq_name": _str(r.get("seq_name")),
            "step_name": _str(r.get("step_name")),
            "step_desc": _str(r.get("step_desc")),
            "fault_count": int(r.get("fault_count") or 0),
            "total_downtime_min": _ms_to_min(r.get("total_duration_ms")),
            "avg_downtime_min": _ms_to_min(r.get("avg_duration_ms")),
        }
        for _, r in pareto_df.iterrows()
    ]
    # "Top faults that caused downtime" -> rank by total downtime, keep top 15.
    pareto.sort(key=lambda x: (x["total_downtime_min"] or 0.0), reverse=True)
    pareto = pareto[:15]

    return jsonify({
        "plc": plc,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "availability": availability,
        "pareto": pareto,
    })


@app.server.route("/api/shift_summary")
def api_shift_summary():
    """GET /api/shift_summary?plc=<name>&start=<iso>&end=<iso>"""
    api_key = os.environ.get("PASSDOWN_API_KEY", "").strip()
    if api_key and request.headers.get("X-API-Key", "").strip() != api_key:
        return jsonify({"error": "unauthorized"}), 401
    plc = (request.args.get("plc") or "").strip()
    if not plc:
        return jsonify({"error": "plc query parameter is required"}), 400
    try:
        start = _parse_iso_utc(request.args.get("start"))
        end = _parse_iso_utc(request.args.get("end"))
    except ValueError:
        now_local = datetime.datetime.now(_app_tz())
        start_local = now_local.replace(hour=7, minute=0, second=0, microsecond=0)
        end_local = min(now_local.replace(hour=15, minute=30, second=0, microsecond=0), now_local)
        if end_local < start_local:
            end_local = start_local
        start = start_local.astimezone(datetime.timezone.utc)
        end = end_local.astimezone(datetime.timezone.utc)
    return jsonify(_build_shift_summary_payload(plc, start, end))


if __name__ == "__main__":
    # DASH_DEBUG=true enables Dash's dev server (hot-reload, verbose errors).
    # Defaults to True so `python app/main.py` for local development still
    # hot-reloads.  docker-compose.yml sets DASH_DEBUG=false so containers
    # never expose the dev server.
    debug = os.environ.get("DASH_DEBUG", "true").lower() in ("true", "1", "yes")
    app.run(debug=debug, host="0.0.0.0", port=8050)
