"""
Top-level Dash layout.

Structure:
    navbar     — brand + refresh / config buttons
    app-shell
      sidebar  — PLC selector + view navigation ONLY
      main     — per-view contextual toolbar + view content
    station detail modal

Every view owns a toolbar with just its controls; only the active view's
toolbar is visible.  View content is a thin shell (#<view>-content) filled
by a dedicated callback that listens only to that view's own controls — so
touching one view's settings can never re-render a different view (the old
layout routed ten inputs into a single render callback, which made every
control change re-query and rebuild the whole page).
"""
from __future__ import annotations

import datetime
import os
from zoneinfo import ZoneInfo

import dash_bootstrap_components as dbc
from dash import dcc, html

TAB_STYLE = {"padding": "6px 14px"}

# view value → (nav button id, nav label)
NAV_VIEWS: dict[str, tuple[str, str]] = {
    "live-status":           ("view-live-status-btn",           "Live Status"),
    "availability-overview": ("view-availability-overview-btn", "Availability Overview"),
    "daily-digest":          ("view-daily-digest-btn",          "Daily Digest"),
    "mod-bottleneck":        ("view-mod-bottleneck-btn",        "Bottleneck Today"),
    "line-issues":           ("view-line-issues-btn",           "Line Issues Timeline"),
    "analysis":              ("view-analysis-btn",              "Analysis"),
}


def _app_tz() -> datetime.tzinfo:
    tz_name = os.environ.get("APP_TIMEZONE", "America/Chicago")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return datetime.timezone.utc


def _toolbar_field(label: str, control, grow: bool = False) -> html.Div:
    return html.Div(
        [html.Div(label, className="toolbar-label"), control],
        className="toolbar-field" + (" toolbar-field-grow" if grow else ""),
    )


def _build_toolbars(start_value: str, end_value: str) -> list[html.Div]:
    """One toolbar per view; visibility is toggled by the nav callback."""
    return [
        html.Div(
            html.Span(
                "Cards update automatically every 30 seconds.",
                className="toolbar-hint",
            ),
            id="toolbar-live-status",
            className="view-toolbar",
        ),
        html.Div(
            [
                _toolbar_field(
                    "Window",
                    dcc.Dropdown(
                        id="avail-overview-hours",
                        options=[
                            {"label": "Last 1 hour", "value": 1},
                            {"label": "Last 4 hours", "value": 4},
                            {"label": "Last 8 hours", "value": 8},
                            {"label": "Last 24 hours", "value": 24},
                            {"label": "Last 7 days", "value": 168},
                        ],
                        value=24,
                        clearable=False,
                    ),
                ),
            ],
            id="toolbar-availability-overview",
            className="view-toolbar d-none",
        ),
        html.Div(
            [
                _toolbar_field(
                    "Shift window",
                    dcc.Dropdown(
                        id="daily-digest-shift-hours",
                        options=[
                            {"label": "8-hour shift", "value": 8},
                            {"label": "12-hour shift", "value": 12},
                            {"label": "24-hour day", "value": 24},
                        ],
                        value=8,
                        clearable=False,
                    ),
                ),
                html.Div(
                    dbc.Button(
                        "Export JSON",
                        id="daily-digest-export-btn",
                        color="secondary",
                        size="sm",
                    ),
                    className="toolbar-actions",
                ),
            ],
            id="toolbar-daily-digest",
            className="view-toolbar d-none",
        ),
        html.Div(
            [
                _toolbar_field(
                    "Exclude stations",
                    dcc.Dropdown(
                        id="bottleneck-excluded-stations",
                        options=[],
                        value=[],
                        multi=True,
                        placeholder="None excluded",
                    ),
                    grow=True,
                ),
            ],
            id="toolbar-mod-bottleneck",
            className="view-toolbar d-none",
        ),
        html.Div(
            [
                _toolbar_field(
                    "PLCs",
                    dcc.Dropdown(
                        id="line-issues-plc-filter",
                        options=[],
                        value=[],
                        multi=True,
                        placeholder="All PLCs",
                    ),
                    grow=True,
                ),
                _toolbar_field(
                    "Exclude stations",
                    dcc.Dropdown(
                        id="line-issues-excluded-stations",
                        options=[],
                        value=[],
                        multi=True,
                        placeholder="None excluded",
                    ),
                    grow=True,
                ),
                _toolbar_field(
                    "Start",
                    dbc.Input(
                        id="line-issues-start-dt",
                        type="datetime-local",
                        value=start_value,
                        size="sm",
                    ),
                ),
                _toolbar_field(
                    "End",
                    dbc.Input(
                        id="line-issues-end-dt",
                        type="datetime-local",
                        value=end_value,
                        size="sm",
                    ),
                ),
            ],
            id="toolbar-line-issues",
            className="view-toolbar d-none",
        ),
        html.Div(
            id="toolbar-analysis",
            className="view-toolbar d-none",
        ),
    ]


def build_layout(plc_names: list[str]) -> html.Div:
    default_plc = plc_names[0] if plc_names else None
    now = datetime.datetime.now(_app_tz())
    start = now - datetime.timedelta(hours=8)
    start_value = start.strftime("%Y-%m-%dT%H:%M")
    end_value = now.strftime("%Y-%m-%dT%H:%M")

    nav_buttons = [
        html.Button(label, id=btn_id, className="sidebar-nav-btn")
        for view, (btn_id, label) in NAV_VIEWS.items()
    ]

    return html.Div(
        [
            # ── Header ──────────────────────────────────────────────────────
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
                    html.Div(
                        className="app-nav-controls",
                        children=[
                            html.Button(
                                "☰",
                                id="sidebar-toggle-btn",
                                className="app-nav-btn",
                                title="Toggle sidebar",
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

            # ── Shell: sidebar (nav only) + main ────────────────────────────
            html.Div(
                id="app-shell",
                className="app-shell",
                children=[
                    html.Aside(
                        id="app-sidebar",
                        className="app-sidebar",
                        children=[
                            html.Div(
                                className="sidebar-section",
                                children=[
                                    html.Div("PLC", className="sidebar-section-title"),
                                    dcc.Dropdown(
                                        id="plc-select",
                                        options=[{"label": n, "value": n} for n in plc_names],
                                        value=default_plc,
                                        clearable=False,
                                        className="app-plc-select",
                                    ),
                                ],
                            ),
                            html.Div(
                                className="sidebar-section",
                                children=[
                                    html.Div("Views", className="sidebar-section-title"),
                                    *nav_buttons,
                                ],
                            ),
                        ],
                    ),
                    html.Main(
                        id="app-main",
                        className="app-main",
                        children=[
                            dbc.Container(
                                [
                                    *_build_toolbars(start_value, end_value),
                                    html.Div(
                                        id="dashboard-main-content",
                                        className="pt-2",
                                    ),
                                ],
                                fluid=True,
                            ),
                        ],
                    ),
                ],
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
                                    dbc.Tab(
                                        label="Runtime", tab_id="runtime-transitions",
                                        tabClassName="fw-semibold", label_style=TAB_STYLE,
                                    ),
                                ],
                                id="modal-tabs",
                                active_tab="step-history",
                            ),
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
            dcc.Store(id="dashboard-view", data="live-status"),
            dcc.Store(id="sidebar-collapsed", data=False),
            html.Div(id="live-timer-noop", style={"display": "none"}),
            dcc.Download(id="daily-digest-export-download"),
            dcc.Interval(id="live-interval",   interval=30_000, n_intervals=0),
            dcc.Interval(id="status-interval", interval=1_000,  n_intervals=0),
        ]
    )
