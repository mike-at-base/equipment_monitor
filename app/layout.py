"""
Top-level Dash layout: full-width live overview + station detail modal.
Tabs and sidebar removed — station cards are the navigation entry point.
"""
from __future__ import annotations

import datetime
import os
from zoneinfo import ZoneInfo

import dash_bootstrap_components as dbc
from dash import dcc, html

TAB_STYLE = {"padding": "6px 14px"}


def _app_tz() -> datetime.tzinfo:
    tz_name = os.environ.get("APP_TIMEZONE", "America/Chicago")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return datetime.timezone.utc


def build_layout(plc_names: list[str]) -> html.Div:
    default_plc = plc_names[0] if plc_names else None
    now   = datetime.datetime.now(_app_tz())
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
                                    dbc.Button(
                                        "↻", id="refresh-btn", color="secondary",
                                        size="sm", title="Refresh",
                                    ),
                                    width="auto",
                                ),
                                dbc.Col(
                                    dbc.Button(
                                        "⚙", id="config-btn", color="secondary",
                                        size="sm", title="Configuration",
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

            # ── Live overview ────────────────────────────────────────────────
            dbc.Container(
                html.Div(id="live-grid-content", className="pt-3"),
                fluid=True,
            ),

            # ── Station detail modal ─────────────────────────────────────────
            dbc.Modal(
                [
                    dbc.ModalHeader(
                        html.Span(id="modal-station-title"),
                        close_button=True,
                    ),
                    dbc.ModalBody(
                        [
                            # Date range toolbar
                            dbc.Row(
                                dbc.Col(
                                    dcc.DatePickerRange(
                                        id="modal-date-range",
                                        start_date=start.date(),
                                        end_date=now.date(),
                                        display_format="YYYY-MM-DD",
                                        updatemode="bothdates",
                                    ),
                                    width="auto",
                                ),
                                className="mb-3",
                            ),
                            # Detail tabs
                            dbc.Tabs(
                                [
                                    dbc.Tab(
                                        label="Step History", tab_id="step-history",
                                        tabClassName="fw-semibold", label_style=TAB_STYLE,
                                    ),
                                    dbc.Tab(
                                        label="Cycle Time", tab_id="cycle-time",
                                        tabClassName="fw-semibold", label_style=TAB_STYLE,
                                    ),
                                    dbc.Tab(
                                        label="Faults", tab_id="faults",
                                        tabClassName="fw-semibold", label_style=TAB_STYLE,
                                    ),
                                    dbc.Tab(
                                        label="Availability", tab_id="availability",
                                        tabClassName="fw-semibold", label_style=TAB_STYLE,
                                    ),
                                ],
                                id="modal-tabs",
                                active_tab="step-history",
                            ),
                            # Tab content with loading spinner
                            dcc.Loading(
                                html.Div(id="modal-tab-content", className="mt-3"),
                                type="circle",
                                color="#b2dd79",
                            ),
                        ]
                    ),
                ],
                id="station-modal",
                size="xl",
                scrollable=True,
                is_open=False,
            ),

            # ── Config modal ─────────────────────────────────────────────────
            dbc.Modal(
                [
                    dbc.ModalHeader(
                        dbc.ModalTitle("Configuration"),
                        close_button=True,
                    ),
                    dbc.ModalBody(html.Div(id="config-modal-body")),
                ],
                id="config-modal",
                size="xl",
                scrollable=True,
                is_open=False,
            ),

            # ── Stores + intervals ───────────────────────────────────────────
            dcc.Store(id="modal-station-data", data=None),
            dcc.Interval(id="live-interval",   interval=30_000, n_intervals=0),
            dcc.Interval(id="status-interval", interval=5_000,  n_intervals=0),
        ]
    )
