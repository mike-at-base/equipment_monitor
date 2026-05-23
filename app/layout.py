"""
Top-level Dash layout: header, sidebar, tab content area.
"""
from __future__ import annotations

import datetime

import dash_bootstrap_components as dbc
from dash import dcc, html

# ── Colour palette ────────────────────────────────────────────────────────────
AVAILABLE_COLOR   = "#2ecc71"
UNAVAILABLE_COLOR = "#e74c3c"
FAULT_COLOR       = "#e67e22"
NEUTRAL_COLOR     = "#95a5a6"

TAB_STYLE = {"padding": "6px 14px"}


def build_layout(plc_names: list[str]) -> html.Div:
    default_plc = plc_names[0] if plc_names else None
    now   = datetime.datetime.utcnow()
    start = now - datetime.timedelta(hours=8)

    return html.Div(
        [
            # ── Header ──────────────────────────────────────────────────────
            dbc.Navbar(
                dbc.Container(
                    [
                        dbc.NavbarBrand("⚡ Equipment Monitor", className="fw-bold"),
                        dbc.Row(
                            [
                                dbc.Col(
                                    dcc.Dropdown(
                                        id="plc-select",
                                        options=[{"label": n, "value": n} for n in plc_names],
                                        value=default_plc,
                                        clearable=False,
                                        style={"minWidth": "140px"},
                                    ),
                                    width="auto",
                                ),
                                dbc.Col(
                                    dcc.DatePickerRange(
                                        id="date-range",
                                        start_date=start.date(),
                                        end_date=now.date(),
                                        display_format="YYYY-MM-DD",
                                        updatemode="bothdates",
                                    ),
                                    width="auto",
                                ),
                                dbc.Col(
                                    dbc.Button(
                                        "↻", id="refresh-btn", color="secondary",
                                        size="sm", title="Refresh",
                                    ),
                                    width="auto",
                                ),
                            ],
                            align="center", className="g-2",
                        ),
                    ],
                    fluid=True,
                ),
                color="dark", dark=True, className="mb-0",
            ),

            # ── Body ─────────────────────────────────────────────────────────
            dbc.Container(
                dbc.Row(
                    [
                        # Sidebar
                        dbc.Col(
                            [
                                html.P("Stations", className="fw-semibold mt-3 mb-1 text-muted small"),
                                dbc.RadioItems(
                                    id="station-filter",
                                    options=[],
                                    value=None,
                                    className="station-list",
                                ),
                                html.Hr(),
                                html.P("EM", className="fw-semibold mb-1 text-muted small"),
                                dbc.Checklist(
                                    id="em-label-filter",
                                    options=[
                                        {"label": "main", "value": "main"},
                                        {"label": "RB01",  "value": "RB01"},
                                        {"label": "RB02",  "value": "RB02"},
                                    ],
                                    value=["main", "RB01", "RB02"],
                                    className="em-checklist",
                                ),
                            ],
                            width=2,
                            className="border-end bg-light",
                            style={"minHeight": "calc(100vh - 56px)", "paddingRight": "12px"},
                        ),

                        # Main content
                        dbc.Col(
                            [
                                dbc.Tabs(
                                    [
                                        dbc.Tab(label="◉ Live Status", tab_id="live-status",   tabClassName="fw-semibold", label_style=TAB_STYLE),
                                        dbc.Tab(label="Step History",  tab_id="step-history",  tabClassName="fw-semibold", label_style=TAB_STYLE),
                                        dbc.Tab(label="Cycle Time",    tab_id="cycle-time",    tabClassName="fw-semibold", label_style=TAB_STYLE),
                                        dbc.Tab(label="Faults",        tab_id="faults",        tabClassName="fw-semibold", label_style=TAB_STYLE),
                                        dbc.Tab(label="Availability",  tab_id="availability",  tabClassName="fw-semibold", label_style=TAB_STYLE),
                                        dbc.Tab(label="⚙ Config",      tab_id="configuration", tabClassName="fw-semibold", label_style=TAB_STYLE),
                                    ],
                                    id="main-tabs",
                                    active_tab="live-status",
                                    className="mt-2",
                                ),
                                html.Div(id="tab-content", className="mt-3"),
                            ],
                            width=10,
                        ),
                    ],
                    className="g-0",
                ),
                fluid=True,
            ),

            # Shared stores
            dcc.Store(id="selected-em-ids", data=[]),
            dcc.Interval(id="live-interval",   interval=30_000, n_intervals=0),
            dcc.Interval(id="status-interval", interval=5_000,  n_intervals=0),
        ]
    )
