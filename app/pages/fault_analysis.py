"""
Fault Analysis page — Pareto chart + fault frequency + detail table.
"""
from __future__ import annotations

import datetime

import dash_bootstrap_components as dbc
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import dash_table, dcc, html

import db.queries as q
from app.brand import DT_STYLE_CELL, DT_STYLE_FILTER, DT_STYLE_HEADER, DT_STYLE_TABLE


def render(em_ids: list[int], start: datetime.datetime, end: datetime.datetime) -> html.Div:
    if not em_ids:
        return html.Div("Select at least one station.", className="text-muted p-3")

    pareto_df = q.query_fault_pareto(em_ids, None, start, end)
    events_df = q.query_fault_events(em_ids, None, start, end)
    freq_df   = q.query_fault_frequency(em_ids, start, end)

    if pareto_df.empty:
        return html.Div("No fault data for selected filters.", className="text-muted p-3")

    total_faults = int(pareto_df["fault_count"].sum())

    # ── Pareto chart ────────────────────────────────────────────────────────
    pareto_df = pareto_df.copy()
    pareto_df["label"] = pareto_df.apply(
        lambda r: r["step_name"] + (f" — {r['step_desc']}" if r["step_desc"] else ""),
        axis=1,
    )
    pareto_df["cumulative_pct"] = (
        pareto_df["fault_count"].cumsum() / total_faults * 100
    )

    fig_pareto = go.Figure()
    fig_pareto.add_trace(go.Bar(
        x=pareto_df["label"],
        y=pareto_df["fault_count"],
        name="Fault count",
        marker_color="#e74c3c",
        yaxis="y",
    ))
    fig_pareto.add_trace(go.Scatter(
        x=pareto_df["label"],
        y=pareto_df["cumulative_pct"],
        name="Cumulative %",
        mode="lines+markers",
        line=dict(color="#2980b9", width=2),
        marker=dict(size=6),
        yaxis="y2",
    ))
    fig_pareto.add_hline(y=80, line_dash="dot", line_color="#2980b9",
                         annotation_text="80%", yref="y2",
                         annotation_position="top right")
    fig_pareto.update_layout(
        title=f"Fault Pareto — {total_faults:,} faults total",
        xaxis=dict(tickangle=-35),
        yaxis=dict(title="Fault Count"),
        yaxis2=dict(title="Cumulative %", overlaying="y", side="right",
                    range=[0, 105], ticksuffix="%"),
        legend=dict(orientation="h", y=1.05),
        margin=dict(l=0, r=0, t=60, b=120),
        height=420,
        bargap=0.2,
    )

    # ── Fault frequency (faults / hour) ─────────────────────────────────────
    fig_freq = go.Figure()
    if not freq_df.empty:
        fig_freq.add_trace(go.Bar(
            x=freq_df["bucket"],
            y=freq_df["fault_count"],
            name="Faults",
            marker_color="#e67e22",
        ))
    fig_freq.update_layout(
        title="Fault Frequency (per hour)",
        xaxis_title=None,
        yaxis_title="Faults",
        margin=dict(l=0, r=10, t=50, b=20),
        height=260,
    )

    # ── Detail table ────────────────────────────────────────────────────────
    if not events_df.empty:
        disp = events_df.copy()
        for col in ["fault_start", "fault_end"]:
            if col in disp.columns:
                disp[col] = pd.to_datetime(disp[col]).dt.strftime("%Y-%m-%d %H:%M:%S")
        disp["duration"] = disp["duration_ms"].apply(
            lambda ms: f"{int(ms/1000)}s" if pd.notna(ms) else "active"
        )
        disp = disp.rename(columns={
            "fault_start": "Start", "fault_end": "End", "duration": "Duration",
            "station": "Station", "em_label": "EM", "seq_name": "Seq",
            "step_name": "Step", "step_desc": "Description",
            "ext_fault_msg": "Fault Message",
        })
        disp = disp.drop(columns=["duration_ms"], errors="ignore")

        table = dash_table.DataTable(
            data=disp.to_dict("records"),
            columns=[{"name": c, "id": c} for c in disp.columns],
            page_size=15,
            sort_action="native",
            filter_action="native",
            style_table=DT_STYLE_TABLE,
            style_cell=DT_STYLE_CELL,
            style_header=DT_STYLE_HEADER,
            style_filter=DT_STYLE_FILTER,
        )
        detail = html.Div([html.H6("Fault Events", className="mt-3"), table])
    else:
        detail = html.Div()

    return html.Div([
        dbc.Row([
            dbc.Col(dcc.Graph(figure=fig_pareto, config={"displayModeBar": False}), md=8),
            dbc.Col(dcc.Graph(figure=fig_freq,   config={"displayModeBar": False}), md=4),
        ]),
        detail,
    ])
