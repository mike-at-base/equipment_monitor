"""
Cycle Time page.

Cycles are detected by watching for steps returning to SEQUENCE_INITIAL_STEP.
The time between two consecutive SEQUENCE_INITIAL_STEP arrivals = one cycle.
"""
from __future__ import annotations

import datetime

import dash_bootstrap_components as dbc
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import dcc, html

import db.queries as q
from db.connection import Conn as _Conn


def render(em_ids: list[int], start: datetime.datetime, end: datetime.datetime) -> html.Div:
    if not em_ids:
        return html.Div("Select at least one station.", className="text-muted p-3")

    # Find production sequences for the selected EMs
    prod_combos: list[tuple[int, int, str, str]] = []  # (em_id, seq_idx, station, seq_name)
    for em_id in em_ids:
        seqs = q.get_sequences_for_em(em_id)
        for s in seqs:
            if s["is_production"]:
                # get station name
                with _Conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT station, display_name FROM config_em WHERE id=%s", (em_id,)
                    )
                    row = cur.fetchone()
                station = row[0] if row else str(em_id)
                label   = row[1] if row else str(em_id)
                prod_combos.append((em_id, s["seq_index"], label, s["seq_name"]))

    if not prod_combos:
        return html.Div(
            "No production sequences configured for selected EMs. "
            "Set is_production: true in config.yaml.",
            className="text-muted p-3",
        )

    tabs = []
    for em_id, seq_idx, label, seq_name in prod_combos:
        df = q.query_cycle_times(em_id, seq_idx, start, end)
        tab_label = f"{label} / {seq_name}"

        if df.empty or len(df) < 2:
            content = html.Div(f"Not enough data for {tab_label}.", className="text-muted")
        else:
            df["cycle_s"] = df["cycle_ms"] / 1000
            # Rolling 10-cycle average
            df["rolling"] = df["cycle_s"].rolling(10, min_periods=1).mean()

            mean_s  = df["cycle_s"].mean()
            std_s   = df["cycle_s"].std()
            min_s   = df["cycle_s"].min()
            max_s   = df["cycle_s"].max()
            count   = len(df)

            # Scatter + rolling avg
            fig_trend = go.Figure()
            fig_trend.add_trace(go.Scatter(
                x=df["ts"], y=df["cycle_s"],
                mode="markers", name="Cycle", marker=dict(size=5, opacity=0.5),
            ))
            fig_trend.add_trace(go.Scatter(
                x=df["ts"], y=df["rolling"],
                mode="lines", name="Rolling avg (10)", line=dict(width=2),
            ))
            fig_trend.add_hline(y=mean_s, line_dash="dash",
                                annotation_text=f"Mean {mean_s:.1f}s",
                                annotation_position="bottom right")
            fig_trend.update_layout(
                title=f"Cycle Time — {tab_label}",
                xaxis_title=None, yaxis_title="Cycle time (s)",
                legend=dict(orientation="h", y=1.02),
                margin=dict(l=0, r=10, t=50, b=20),
                height=320,
            )

            # Histogram
            fig_hist = go.Figure()
            fig_hist.add_trace(go.Histogram(x=df["cycle_s"], nbinsx=40, name="Count"))
            fig_hist.add_vline(x=mean_s, line_dash="dash",
                               annotation_text=f"Mean {mean_s:.1f}s")
            fig_hist.update_layout(
                title="Cycle Time Distribution",
                xaxis_title="Cycle time (s)", yaxis_title="Count",
                margin=dict(l=0, r=10, t=50, b=20),
                height=280,
            )

            stats = dbc.Card(
                dbc.CardBody([
                    html.H6("Statistics", className="card-title"),
                    dbc.Row([
                        dbc.Col([html.P("Count",  className="text-muted mb-0 small"), html.H5(f"{count:,}")]),
                        dbc.Col([html.P("Mean",   className="text-muted mb-0 small"), html.H5(f"{mean_s:.2f}s")]),
                        dbc.Col([html.P("Std Dev",className="text-muted mb-0 small"), html.H5(f"{std_s:.2f}s")]),
                        dbc.Col([html.P("Min",    className="text-muted mb-0 small"), html.H5(f"{min_s:.2f}s")]),
                        dbc.Col([html.P("Max",    className="text-muted mb-0 small"), html.H5(f"{max_s:.2f}s")]),
                    ]),
                ]),
                className="mb-3",
            )

            content = html.Div([
                stats,
                dcc.Graph(figure=fig_trend, config={"displayModeBar": False}),
                dcc.Graph(figure=fig_hist,  config={"displayModeBar": False}),
            ])

        tabs.append(dbc.Tab(content, label=tab_label, tab_id=f"ct-{em_id}-{seq_idx}"))

    if len(tabs) == 1:
        return html.Div(tabs[0].children)

    return dbc.Tabs(tabs, active_tab=tabs[0].tab_id)
