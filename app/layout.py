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
    start_value = start.strftime("%Y-%m-%dT%H:%M")
    end_value = now.strftime("%Y-%m-%dT%H:%M")

    return html.Div(
        [
            # ── Header ──────────────────────────────────────────────────────
            # Maxestro-style nav: Base logo + vertical divider + uppercase
            # title on the left; PLC selector + icon buttons on the right.
            # All styling lives in app/assets/base.css under .app-navbar.
            html.Nav(
                className="app-navbar",
                children=[
                    html.Span(
                        className="app-brand",
                        children=[
                            html.Img(
                                src="/assets/base_logo.svg",
                                alt="Base",
                                className="app-brand-logo",
                            ),
                            html.Span(className="app-brand-divider"),
                            html.Span(
                                "Equipment Monitor",
                                className="app-brand-title",
                            ),
                        ],
                    ),
                    # Right-aligned control cluster
                    html.Div(
                        className="app-nav-controls",
                        children=[
                            dcc.Dropdown(
                                id="plc-select",
                                options=[{"label": n, "value": n} for n in plc_names],
                                value=default_plc,
                                clearable=False,
                                className="app-plc-select",
                            ),
                            html.Button(
                                "↻",
                                id="refresh-btn",
                                className="app-nav-btn",
                                title="Refresh",
                            ),
                            html.Button(
                                "⚙",
                                id="config-btn",
                                className="app-nav-btn",
                                title="Configuration",
                            ),
                        ],
                    ),
                ],
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
                            # Date/time toolbar
                            dbc.Row(
                                [
                                    dbc.Col(
                                        dbc.Input(
                                            id="modal-start-dt",
                                            type="datetime-local",
                                            value=start_value,
                                        ),
                                        md=3,
                                    ),
                                    dbc.Col(
                                        dbc.Input(
                                            id="modal-end-dt",
                                            type="datetime-local",
                                            value=end_value,
                                        ),
                                        md=3,
                                    ),
                                    dbc.Col(
                                        html.Div(
                                            [
                                                dbc.Button(
                                                    "Last 1h", id="range-1h-btn",
                                                    color="secondary", size="sm",
                                                    className="me-1",
                                                ),
                                                dbc.Button(
                                                    "Last 4h", id="range-4h-btn",
                                                    color="secondary", size="sm",
                                                    className="me-1",
                                                ),
                                                dbc.Button(
                                                    "Last 8h", id="range-8h-btn",
                                                    color="secondary", size="sm",
                                                    className="me-1",
                                                ),
                                                dbc.Button(
                                                    "Last 24h", id="range-24h-btn",
                                                    color="secondary", size="sm",
                                                ),
                                            ],
                                            className="d-flex align-items-center h-100",
                                        ),
                                        md=6,
                                    ),
                                ],
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
