"""
Cycle Time page.

Cycle time is start-to-start on the production sequence:
time between consecutive ARRIVALS to SEQUENCE_INITIAL_STEP.
"""
from __future__ import annotations

import datetime
import os
from zoneinfo import ZoneInfo

import dash_bootstrap_components as dbc
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import dcc, html

import db.queries as q
from db.connection import Conn as _Conn


def _plant_tz() -> datetime.tzinfo:
    tz_name = os.environ.get("APP_TIMEZONE", "America/Chicago")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return datetime.timezone.utc


def _to_plant_time(ts, tz: datetime.tzinfo):
    if ts is None or pd.isna(ts):
        return ts
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(tz)


def render(em_ids: list[int], start: datetime.datetime, end: datetime.datetime) -> html.Div:
    if not em_ids:
        return html.Div("Select at least one station.", className="text-muted p-3")

    # Find production sequences for the selected EMs
    prod_combos: list[tuple[int, int, str, str, str]] = []  # (em_id, seq_idx, station, seq_name, cycle_start_step)
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
                prod_combos.append((
                    em_id,
                    s["seq_index"],
                    label,
                    s["seq_name"],
                    s.get("cycle_start_step") or "SEQUENCE_INITIAL_STEP",
                ))

    if not prod_combos:
        return html.Div(
            "No production sequences configured for selected EMs. "
            "Set is_production: true in config.yaml.",
            className="text-muted p-3",
        )

    tabs = []
    plant_tz = _plant_tz()
    for em_id, seq_idx, label, seq_name, cycle_start_step in prod_combos:
        df = q.query_cycle_times(em_id, seq_idx, cycle_start_step, start, end)
        tab_label = f"{label} / {seq_name}"

        if df.empty or len(df) < 2:
            content = html.Div(f"Not enough data for {tab_label}.", className="text-muted")
        else:
            # EXTRACT(EPOCH ...) can arrive as Decimal from psycopg; cast to
            # float so pandas stats (std/rolling) don't error on mixed types.
            df["cycle_ms"] = pd.to_numeric(df["cycle_ms"], errors="coerce")
            df = df.dropna(subset=["cycle_ms"]).copy()
            if df.empty:
                content = html.Div(f"Not enough data for {tab_label}.", className="text-muted")
                tabs.append(dbc.Tab(content, label=tab_label, tab_id=f"ct-{em_id}-{seq_idx}"))
                continue
            df["ts"] = df["ts"].apply(lambda v: _to_plant_time(v, plant_tz))
            df["cycle_s"] = df["cycle_ms"].astype(float) / 1000.0
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
                title=f"Cycle Time (Start-to-Start on {cycle_start_step}) — {tab_label}",
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
