"""
Step History page — table of step events + average step-time bar chart.
"""
from __future__ import annotations

import datetime

import dash_bootstrap_components as dbc
import pandas as pd
import plotly.express as px
from dash import dash_table, dcc, html

import db.queries as q
from app.brand import DT_STYLE_CELL, DT_STYLE_FILTER, DT_STYLE_HEADER, DT_STYLE_TABLE, FAULTED_COND


def _fmt_ms(ms) -> str:
    if ms is None or pd.isna(ms):
        return "—"
    ms = int(ms)
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms/1000:.2f} s"
    return f"{ms/60_000:.1f} min"


def render(em_ids: list[int], start: datetime.datetime, end: datetime.datetime) -> html.Div:
    if not em_ids:
        return html.Div("Select at least one station.", className="text-muted p-3")

    df = q.query_step_history(em_ids, None, start, end, limit=3000)

    if df.empty:
        return html.Div("No step data for selected filters.", className="text-muted p-3")

    # ── Table ──────────────────────────────────────────────────────────────
    display = df[["ts", "station", "em_label", "seq_name",
                  "step_name", "step_desc", "duration_ms", "was_faulted"]].copy()
    display["ts"] = display["ts"].dt.strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3]
    display["Duration"] = display["duration_ms"].apply(_fmt_ms)
    display = display.rename(columns={
        "ts": "Timestamp", "station": "Station", "em_label": "EM",
        "seq_name": "Sequence", "step_name": "Step",
        "step_desc": "Description", "was_faulted": "Faulted",
    })
    display = display.drop(columns=["duration_ms"])
    # Column order: put Duration right after Description, before Faulted
    display = display[["Timestamp", "Station", "EM", "Sequence",
                        "Step", "Description", "Duration", "Faulted"]]

    table = dash_table.DataTable(
        data=display.to_dict("records"),
        columns=[{"name": c, "id": c} for c in display.columns],
        page_size=25,
        sort_action="native",
        filter_action="native",
        style_table=DT_STYLE_TABLE,
        style_cell=DT_STYLE_CELL,
        style_header=DT_STYLE_HEADER,
        style_filter=DT_STYLE_FILTER,
        style_data_conditional=[FAULTED_COND],
    )

    # ── Bar chart: average time per step (production seq only) ─────────────
    prod_df = df[df["duration_ms"].notna() & (df["duration_ms"] > 0)].copy()
    if not prod_df.empty:
        avg = (
            prod_df.groupby(["step_name", "step_desc"], dropna=False)["duration_ms"]
            .mean()
            .reset_index()
            .sort_values("duration_ms", ascending=True)
        )
        avg["label"] = avg.apply(
            lambda r: r["step_name"] + (f" — {r['step_desc']}" if r["step_desc"] else ""),
            axis=1,
        )
        fig = px.bar(
            avg, x="duration_ms", y="label", orientation="h",
            labels={"duration_ms": "Avg Duration (ms)", "label": "Step"},
            title="Average Step Duration",
            height=max(300, len(avg) * 24),
        )
        fig.update_layout(margin=dict(l=0, r=10, t=40, b=20), yaxis_title=None)
        chart = dcc.Graph(figure=fig, config={"displayModeBar": False})
    else:
        chart = html.Div()

    return html.Div([
        dbc.Row([
            dbc.Col(html.Small(f"{len(df):,} events", className="text-muted"), width="auto"),
        ], className="mb-2"),
        dbc.Row([
            dbc.Col(table,  md=7),
            dbc.Col(chart,  md=5),
        ]),
    ])
