"""
Step History page — table of step events + average step-duration bar chart.

The bar chart has a slider to exclude outlier samples.  A step that
occasionally takes 10 min when it normally takes 5 s otherwise drags its
average to ~3 s, hiding the underlying behaviour.  Dragging the slider down
recomputes the per-step mean using only samples ≤ the cutoff.
"""
from __future__ import annotations

import datetime
import math
import os
from zoneinfo import ZoneInfo

import dash_bootstrap_components as dbc
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import dash_table, dcc, html

import db.queries as q
from app.brand import (
    DT_STYLE_CELL, DT_STYLE_FILTER, DT_STYLE_HEADER, DT_STYLE_TABLE,
    FAULTED_COND, MUTED,
)


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


def _fmt_ms(ms) -> str:
    if ms is None or pd.isna(ms):
        return "—"
    ms = int(ms)
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms/1000:.2f} s"
    return f"{ms/60_000:.1f} min"


def build_step_duration_figure(
    prod_df: pd.DataFrame,
    max_duration_ms: int | None = None,
) -> go.Figure:
    """
    Build the "Average Step Duration" horizontal bar chart from raw step
    samples.

    If ``max_duration_ms`` is provided, samples with ``duration_ms`` exceeding
    it are excluded before computing the per-step mean.  This is the
    outlier-filter slider's hook into the chart: dragging the slider sets a
    new cutoff, and the bars recompute to reflect typical (not pathological)
    behaviour.
    """
    df = prod_df
    if max_duration_ms is not None:
        df = df[df["duration_ms"] <= max_duration_ms]

    if df.empty:
        # Empty figure — keep the chart container at a sensible height so the
        # layout doesn't collapse while the user drags the slider to zero.
        fig = go.Figure()
        fig.update_layout(
            title="Average Step Duration (no samples in range)",
            margin=dict(l=0, r=10, t=40, b=20),
            height=300,
        )
        return fig

    # Aggregate by step_name only — keeps the y-axis labels short and
    # scannable.  Description goes into the hover via custom_data.  When the
    # same step_name appears with multiple descriptions (rare but possible),
    # the first non-empty one wins.
    avg = (
        df.groupby("step_name", dropna=False)
        .agg(
            duration_ms=("duration_ms", "mean"),
            step_desc=("step_desc",
                       lambda s: next((d for d in s if pd.notna(d) and d), "")),
            sample_count=("duration_ms", "count"),
        )
        .reset_index()
        .sort_values("duration_ms", ascending=True)
    )
    # Empty-description fallback so the hover doesn't read "—None—"
    avg["step_desc"] = avg["step_desc"].fillna("").replace("", "—")

    n_samples = len(df)
    title = f"Average Step Duration — {n_samples:,} samples"
    if max_duration_ms is not None:
        cutoff_s = max_duration_ms / 1000
        cutoff_label = (
            f"{cutoff_s:.0f}s" if cutoff_s < 60 else f"{cutoff_s/60:.1f} min"
        )
        title += f" (≤ {cutoff_label})"

    fig = px.bar(
        avg,
        x="duration_ms",
        y="step_name",
        orientation="h",
        labels={"duration_ms": "Avg Duration (ms)", "step_name": "Step"},
        title=title,
        height=max(300, len(avg) * 24),
        custom_data=["step_desc", "sample_count"],
    )
    # Hover format:  STEP_NAME (bold)
    #                Description
    #                Avg: 1,234 ms  ·  42 samples
    fig.update_traces(
        hovertemplate=(
            "<b>%{y}</b><br>"
            "%{customdata[0]}<br>"
            "Avg: %{x:,.0f} ms  ·  %{customdata[1]:,} samples"
            "<extra></extra>"
        ),
    )
    # automargin=True lets Plotly reserve enough left-side space for the
    # longest step name; without it, margin.l=0 would clip the y-axis tick
    # labels entirely (the bars render but the names don't).
    fig.update_layout(
        margin=dict(l=10, r=10, t=40, b=20),
        yaxis_title=None,
    )
    fig.update_yaxes(automargin=True)
    return fig


def _build_slider_marks(max_s: int) -> dict[int, str]:
    """
    Pick sensible labelled marks for a 0..max_s second slider.  Always
    includes 0 and max_s; intermediate marks chosen from typical breakpoints
    that fit within the range.
    """
    candidates = [1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600]
    marks: dict[int, str] = {0: "0"}
    for s in candidates:
        if s < max_s:
            marks[s] = f"{s}s" if s < 60 else f"{s // 60}m"
    marks[max_s] = "all"
    return marks


def render(em_ids: list[int], start: datetime.datetime,
           end: datetime.datetime) -> html.Div:
    if not em_ids:
        return html.Div("Select at least one station.", className="text-muted p-3")

    df = q.query_step_history(em_ids, None, start, end, limit=3000)

    if df.empty:
        return html.Div("No step data for selected filters.", className="text-muted p-3")

    # ── Table ──────────────────────────────────────────────────────────────
    display = df[["ts", "station", "em_label", "seq_name",
                  "step_name", "step_desc", "duration_ms", "was_faulted"]].copy()
    plant_tz = _plant_tz()
    display["ts"] = display["ts"].apply(lambda v: _to_plant_time(v, plant_tz))
    # 12-hour clock with millisecond precision: "2026-05-23 02:30:45.123 PM"
    display["ts"] = (
        display["ts"].dt.strftime("%Y-%m-%d %I:%M:%S.%f").str[:-3]
        + " " + display["ts"].dt.strftime("%p")
    )
    display["Duration"] = display["duration_ms"].apply(_fmt_ms)
    # Convert boolean to lowercase string so filter_query string comparison works
    display["was_faulted"] = display["was_faulted"].map({True: "yes", False: ""})
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

    # ── Average step duration chart + outlier slider ────────────────────────
    prod_df = df[df["duration_ms"].notna() & (df["duration_ms"] > 0)].copy()

    if prod_df.empty:
        chart_section = html.Div()
    else:
        # Slider domain spans 0 → longest sample (rounded up to whole seconds).
        # The user can drag down to exclude the long tail.
        max_ms = int(prod_df["duration_ms"].max())
        max_s  = max(1, math.ceil(max_ms / 1000))
        marks  = _build_slider_marks(max_s)

        # Initial figure uses the full dataset (slider at max = no filter)
        initial_fig = build_step_duration_figure(prod_df)

        # Only the columns the callback needs go into the client store, so we
        # don't ship the full row payload (timestamps, descriptions, etc.).
        store_data = prod_df[
            ["step_name", "step_desc", "duration_ms"]
        ].to_dict("records")

        slider_row = dbc.Row([
            dbc.Col([
                html.Small(
                    "Exclude samples longer than:",
                    className="text-muted d-block mb-1",
                ),
                dcc.Slider(
                    id="step-dur-slider",
                    min=0, max=max_s, step=1, value=max_s,
                    marks=marks,
                    tooltip={
                        "placement": "bottom",
                        "always_visible": True,
                        # Newer Dash supports template substitution; if your
                        # Dash version ignores 'template', the tooltip just
                        # falls back to the raw number — still informative.
                        "template": "≤ {value} s",
                    },
                    updatemode="mouseup",
                ),
            ], md=10),
        ], className="mt-4 mb-2 px-2")

        chart_section = html.Div([
            # Store keyed by id so the main.py callback can read it.
            dcc.Store(id="step-dur-data", data=store_data),
            slider_row,
            dcc.Loading(
                dcc.Graph(
                    id="step-dur-chart",
                    figure=initial_fig,
                    config={"displayModeBar": False},
                ),
                type="circle", color="#b2dd79",
            ),
        ])

    return html.Div([
        html.Small(f"{len(df):,} events", className="text-muted d-block mb-2"),
        table,
        chart_section,
    ])
