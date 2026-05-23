"""
Configuration page — YAML editor + collector connection status.
"""
from __future__ import annotations

import os

import dash_bootstrap_components as dbc
import yaml
from dash import dcc, html

import db.queries as q
from db.connection import Conn as _Conn

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config.yaml")


def _load_yaml() -> str:
    try:
        with open(CONFIG_PATH) as f:
            return f.read()
    except FileNotFoundError:
        return "# config.yaml not found"


def render() -> html.Div:
    yaml_text = _load_yaml()

    # Collector status
    try:
        status_df = q.get_collector_status()
        if status_df.empty:
            status_section = dbc.Alert(
                "Collector has not reported yet. Start it with: python collector/main.py",
                color="warning",
            )
        else:
            rows = []
            for _, r in status_df.iterrows():
                badge_color = "success" if r["connected"] else "danger"
                badge_text  = "Connected" if r["connected"] else "Disconnected"
                last = r["last_seen"].strftime("%Y-%m-%d %H:%M:%S") if r["last_seen"] else "—"
                rows.append(
                    html.Tr([
                        html.Td(r["plc_name"]),
                        html.Td(dbc.Badge(badge_text, color=badge_color)),
                        html.Td(last),
                        html.Td(r["node_count"]),
                    ])
                )
            status_section = dbc.Table(
                [
                    html.Thead(html.Tr([
                        html.Th("PLC"), html.Th("Status"),
                        html.Th("Last Seen"), html.Th("Nodes"),
                    ])),
                    html.Tbody(rows),
                ],
                bordered=True, size="sm", className="mb-0",
            )
    except Exception as e:
        status_section = dbc.Alert(f"Could not query collector status: {e}", color="danger")

    # EM enable/disable table from DB
    try:
        with _Conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT e.id, p.name AS plc, e.station, e.display_name,
                       e.em_label, e.enabled
                FROM config_em e
                JOIN config_plc p ON p.id = e.plc_id
                ORDER BY p.name, e.station, e.em_label
                """
            )
            em_rows = cur.fetchall()
        em_table = dbc.Table(
            [
                html.Thead(html.Tr([
                    html.Th("PLC"), html.Th("Station"), html.Th("Name"),
                    html.Th("EM"), html.Th("Enabled"),
                ])),
                html.Tbody([
                    html.Tr([
                        html.Td(r[1]), html.Td(r[2]), html.Td(r[3]), html.Td(r[4]),
                        html.Td(dbc.Badge("Yes" if r[5] else "No",
                                          color="success" if r[5] else "secondary")),
                    ])
                    for r in em_rows
                ]),
            ],
            bordered=True, size="sm",
        )
    except Exception as e:
        em_table = dbc.Alert(f"Could not load EM list: {e}", color="danger")

    return html.Div([
        dbc.Row([
            # Left: YAML editor
            dbc.Col([
                html.H6("config.yaml"),
                dcc.Textarea(
                    id="yaml-editor",
                    value=yaml_text,
                    style={"width": "100%", "height": "500px",
                           "fontFamily": "monospace", "fontSize": "12px"},
                ),
                dbc.Button("Save config.yaml", id="yaml-save-btn",
                           color="primary", size="sm", className="mt-2"),
                html.Div(id="yaml-save-status", className="mt-2"),
                dbc.Alert(
                    "⚠ Restart the collector after saving to apply changes.",
                    color="warning", className="mt-2 py-2 small",
                ),
            ], md=7),

            # Right: status panels
            dbc.Col([
                html.H6("Collector Status"),
                status_section,
                html.H6("Equipment Modules", className="mt-4"),
                em_table,
            ], md=5),
        ]),
    ])
