"""
Dash app entry point.
    python app/main.py
Opens at http://localhost:8050
"""
from __future__ import annotations

import datetime
import os
import sys

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, dcc, html
from dash.exceptions import PreventUpdate

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app.brand  # registers Plotly template + exports DT styles
import db.queries as q
from app.layout import build_layout
from app.pages import (
    availability,
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
        return [p["name"] for p in q.get_all_plcs()]
    except Exception:
        return []

app.layout = build_layout(_plc_names() or ["CELL1"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_window(start_date, end_date) -> tuple[datetime.datetime, datetime.datetime]:
    tz = datetime.timezone.utc
    if isinstance(start_date, str):
        start_date = datetime.date.fromisoformat(start_date[:10])
    if isinstance(end_date, str):
        end_date = datetime.date.fromisoformat(end_date[:10])
    start = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=tz)
    end   = datetime.datetime.combine(end_date,   datetime.time.max, tzinfo=tz)
    return start, end


def _get_em_ids(plc_name: str, stations: list[str], em_labels: list[str]) -> list[int]:
    if not plc_name or not stations or not em_labels:
        return []
    with q.Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT e.id
            FROM config_em e
            JOIN config_plc p ON p.id = e.plc_id
            WHERE p.name = %s
              AND e.station  = ANY(%s)
              AND e.em_label = ANY(%s)
              AND e.enabled  = TRUE
            """,
            (plc_name, stations, em_labels),
        )
        return [r[0] for r in cur.fetchall()]


# ── Callbacks ─────────────────────────────────────────────────────────────────

@callback(
    Output("station-filter", "options"),
    Output("station-filter", "value"),
    Input("plc-select", "value"),
)
def update_station_list(plc_name):
    if not plc_name:
        raise PreventUpdate
    stations = q.get_stations_for_plc(plc_name)
    opts = [{"label": s["display_name"], "value": s["station"]} for s in stations]
    val  = stations[0]["station"] if stations else None
    return opts, val


@callback(
    Output("selected-em-ids", "data"),
    Input("plc-select",       "value"),
    Input("station-filter",   "value"),
    Input("em-label-filter",  "value"),
    Input("refresh-btn",      "n_clicks"),
    Input("live-interval",    "n_intervals"),
)
def update_em_ids(plc, station, em_labels, _n, _interval):
    stations = [station] if station else []
    return _get_em_ids(plc or "", stations, em_labels or [])


@callback(
    Output("tab-content", "children"),
    Input("main-tabs",        "active_tab"),
    Input("selected-em-ids",  "data"),
    Input("date-range",       "start_date"),
    Input("date-range",       "end_date"),
    Input("refresh-btn",      "n_clicks"),
    Input("live-interval",    "n_intervals"),
    Input("status-interval",  "n_intervals"),
    State("plc-select",       "value"),
)
def render_tab(active_tab, em_ids, start_date, end_date, _n, _interval,
               _status_interval, plc_name):
    if active_tab == "live-status":
        return station_status.render(plc_name or "")

    if active_tab == "configuration":
        return configuration.render()

    if not em_ids:
        return dbc.Alert("Select at least one station in the sidebar.", color="info")

    start, end = _parse_window(
        start_date or datetime.date.today() - datetime.timedelta(hours=8),
        end_date   or datetime.date.today(),
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
    Output("yaml-save-status", "children"),
    Input("yaml-save-btn", "n_clicks"),
    State("yaml-editor",   "value"),
    prevent_initial_call=True,
)
def save_yaml(n_clicks, content):
    if not n_clicks or not content:
        raise PreventUpdate
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config.yaml"
    )
    try:
        # Validate YAML before writing
        import yaml
        yaml.safe_load(content)
        with open(config_path, "w") as f:
            f.write(content)
        return dbc.Alert("✓ Saved. Restart the collector to apply changes.",
                         color="success", duration=4000)
    except Exception as e:
        return dbc.Alert(f"Error: {e}", color="danger")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8050)
