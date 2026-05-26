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
from dash import ALL, Input, Output, State, callback, ctx, dcc, html
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

def _parse_window(
    start_date, end_date
) -> tuple[datetime.datetime, datetime.datetime]:
    # DatePickerRange emits date-only values in the operator's local context.
    # Convert from plant-local day boundaries to UTC for DB queries so evening
    # local events don't slip into the next UTC day and disappear from charts.
    tz_name = os.environ.get("APP_TIMEZONE", "America/Chicago")
    try:
        local_tz = ZoneInfo(tz_name)
    except Exception:
        local_tz = datetime.timezone.utc
    if isinstance(start_date, str):
        start_date = datetime.date.fromisoformat(start_date[:10])
    if isinstance(end_date, str):
        end_date = datetime.date.fromisoformat(end_date[:10])
    start = datetime.datetime.combine(
        start_date, datetime.time.min, tzinfo=local_tz
    ).astimezone(datetime.timezone.utc)
    end = datetime.datetime.combine(
        end_date, datetime.time.max, tzinfo=local_tz
    ).astimezone(datetime.timezone.utc)
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
    Output("station-modal",      "is_open"),
    Output("modal-station-data", "data"),
    Output("modal-tabs",         "active_tab"),
    Input({"type": "station-card", "index": ALL}, "n_clicks"),
    State("plc-select", "value"),
    prevent_initial_call=True,
)
def open_station_modal(n_clicks_list, plc_name):
    """Open the detail modal when any station card is clicked."""
    if not any(n_clicks_list):
        raise PreventUpdate
    triggered = ctx.triggered_id
    if not isinstance(triggered, dict):
        raise PreventUpdate
    station = triggered["index"]
    return True, {"station": station, "plc": plc_name or ""}, "step-history"


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
    Input("modal-date-range",   "start_date"),
    Input("modal-date-range",   "end_date"),
    Input("modal-station-data", "data"),
)
def render_modal_tab(active_tab, start_date, end_date, data):
    if not data:
        raise PreventUpdate

    station  = data.get("station", "")
    plc_name = data.get("plc", "")
    em_ids   = _get_em_ids_for_station(plc_name, station)

    if not em_ids:
        return dbc.Alert(
            "No equipment modules found for this station.", color="warning"
        )

    today = datetime.date.today()
    start, end = _parse_window(
        start_date or today - datetime.timedelta(days=1),
        end_date   or today,
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
    State("step-dur-data",   "data"),
    prevent_initial_call=True,
)
def update_step_duration_chart(max_seconds, store_data):
    """
    Recompute the Average Step Duration bar chart whenever the outlier slider
    moves.  ``max_seconds`` is the inclusive cutoff in seconds — samples
    longer than this are excluded before the per-step mean is computed.
    """
    if not store_data:
        raise PreventUpdate
    prod_df = pd.DataFrame(store_data)
    max_ms = (max_seconds or 0) * 1000
    return step_history.build_step_duration_figure(
        prod_df, max_duration_ms=max_ms,
    )


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
