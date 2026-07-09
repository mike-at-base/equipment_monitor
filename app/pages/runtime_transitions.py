from __future__ import annotations

import datetime
import os
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
from dash import dash_table, dcc, html

import db.queries as q
from app.brand import DT_STYLE_CELL, DT_STYLE_FILTER, DT_STYLE_HEADER, DT_STYLE_TABLE

RUNTIME_STATE_COLOR = {
    "running": "#b2dd79",
    "paused": "#f7c33c",
    "stopped": "#c95f00",
    "faulted": "#c51808",
    "unknown": "#9a9794",
}
RUNTIME_STATE_ORDER = ["running", "paused", "stopped", "faulted", "unknown"]
FLOW_KIND_COLOR = {
    "blocked": "#6a5acd",
    "starved": "#00a3c4",
}
FLOW_KIND_ORDER = ["blocked", "starved"]


def _app_tz() -> datetime.tzinfo:
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
        return html.Div("No equipment modules selected.", className="text-muted")

    df = q.query_runtime_transitions(em_ids, start, end, limit=5000)
    flow_df = q.query_flow_events(em_ids, start, end, limit=2000)
    if df.empty and flow_df.empty:
        return html.Div("No runtime or blocked/starved events in selected range.", className="text-muted")
    if df.empty:
        df = pd.DataFrame(columns=[
            "ts", "station", "display_name", "em_label", "from_state", "to_state",
            "automatic", "running", "paused", "stopped", "unknown_status", "fault",
            "active_seq", "active_is_production", "step_name", "step_desc",
        ])

    tz = _app_tz()
    start_local = _to_plant_time(start, tz)
    end_local = _to_plant_time(end, tz)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    if not flow_df.empty:
        flow_df["start_ts"] = pd.to_datetime(flow_df["start_ts"], utc=True, errors="coerce")
        flow_df["end_ts"] = pd.to_datetime(flow_df["end_ts"], utc=True, errors="coerce")

    timeline_df = (
        df[["ts", "station", "display_name", "em_label", "to_state"]]
        .sort_values(["station", "em_label", "ts"])
        .copy()
    )
    if not timeline_df.empty:
        timeline_df["next_ts"] = timeline_df.groupby(["station", "em_label"])["ts"].shift(-1)
        end_utc = pd.Timestamp(end)
        if end_utc.tzinfo is None:
            end_utc = end_utc.tz_localize("UTC")
        else:
            end_utc = end_utc.tz_convert("UTC")
        timeline_df["seg_end"] = timeline_df["next_ts"].fillna(end_utc)
        timeline_df = timeline_df[timeline_df["seg_end"] > timeline_df["ts"]]

    label_frames: list[pd.DataFrame] = []
    if not timeline_df.empty:
        label_frames.append(timeline_df[["station", "display_name", "em_label"]])
    if not flow_df.empty:
        label_frames.append(flow_df[["station", "display_name", "em_label"]])
    labels = (
        pd.concat(label_frames, ignore_index=True)
        .drop_duplicates()
        .assign(
            _em_sort=lambda x: x["em_label"].map(lambda v: (0, "") if str(v).lower() == "main" else (1, str(v))),
        )
        .sort_values(["station", "_em_sort", "display_name"])
    )
    y_map = {
        (r["station"], r["em_label"]): f"{r['display_name']} / {r['em_label']}"
        for _, r in labels.iterrows()
    }
    buckets: dict[str, dict] = {
        s: {"x": [], "y": [], "base": [], "hover": []} for s in RUNTIME_STATE_ORDER
    }
    for _, row in timeline_df.iterrows():
        state = str(row.get("to_state") or "unknown").lower()
        if state not in buckets:
            state = "unknown"
        t0 = row["ts"]
        t1 = row["seg_end"]
        t0_local = _to_plant_time(t0, tz)
        t1_local = _to_plant_time(t1, tz)
        y_lbl = y_map.get((row["station"], row["em_label"]), f"{row['display_name']} / {row['em_label']}")
        dur_ms = (t1 - t0).total_seconds() * 1000.0
        b = buckets[state]
        b["x"].append(dur_ms)
        b["y"].append(y_lbl)
        b["base"].append(t0.timestamp() * 1000)
        b["hover"].append(
            f"{state.title()}<br>{row['display_name']} / {row['em_label']}<br>"
            f"{t0_local.strftime('%Y-%m-%d %I:%M:%S %p')} - {t1_local.strftime('%Y-%m-%d %I:%M:%S %p')}"
        )

    flow_buckets: dict[str, dict] = {
        k: {"x": [], "y": [], "base": [], "hover": []} for k in FLOW_KIND_ORDER
    }
    if not flow_df.empty:
        end_utc = pd.Timestamp(end)
        if end_utc.tzinfo is None:
            end_utc = end_utc.tz_localize("UTC")
        else:
            end_utc = end_utc.tz_convert("UTC")
        for _, row in flow_df.iterrows():
            kind = str(row.get("kind") or "").lower()
            if kind not in flow_buckets:
                continue
            t0 = row.get("start_ts")
            if pd.isna(t0):
                continue
            t1 = row.get("end_ts")
            if pd.isna(t1):
                t1 = end_utc
            if t1 <= t0:
                continue
            y_lbl = y_map.get((row["station"], row["em_label"]), f"{row['display_name']} / {row['em_label']}")
            t0_local = _to_plant_time(t0, tz)
            t1_local = _to_plant_time(t1, tz)
            dur_ms = (t1 - t0).total_seconds() * 1000.0
            reason = str(row.get("reason_desc") or "").strip()
            step = str(row.get("step_name") or "").strip()
            extra = []
            if step:
                extra.append(f"Step: {step}")
            if reason:
                extra.append(f"Reason: {reason}")
            extra_txt = "<br>".join(extra)
            if extra_txt:
                extra_txt = "<br>" + extra_txt
            fb = flow_buckets[kind]
            fb["x"].append(dur_ms)
            fb["y"].append(y_lbl)
            fb["base"].append(t0.timestamp() * 1000)
            fb["hover"].append(
                f"{kind.title()}<br>{row['display_name']} / {row['em_label']}<br>"
                f"{t0_local.strftime('%Y-%m-%d %I:%M:%S %p')} - {t1_local.strftime('%Y-%m-%d %I:%M:%S %p')}{extra_txt}"
            )

    fig = go.Figure()
    for state in RUNTIME_STATE_ORDER:
        d = buckets[state]
        if d["x"]:
            fig.add_trace(go.Bar(
                x=d["x"],
                y=d["y"],
                orientation="h",
                base=d["base"],
                marker_color=RUNTIME_STATE_COLOR[state],
                name=state.title(),
                showlegend=True,
                customdata=d["hover"],
                hovertemplate="%{customdata}<extra></extra>",
                width=0.6,
            ))
        else:
            fig.add_trace(go.Bar(
                x=[None],
                y=[None],
                orientation="h",
                marker_color=RUNTIME_STATE_COLOR[state],
                name=state.title(),
                showlegend=True,
            ))
    for kind in FLOW_KIND_ORDER:
        d = flow_buckets[kind]
        if d["x"]:
            fig.add_trace(go.Bar(
                x=d["x"],
                y=d["y"],
                orientation="h",
                base=d["base"],
                marker_color=FLOW_KIND_COLOR[kind],
                marker_pattern_shape="/" if kind == "blocked" else "\\",
                name=f"{kind.title()} (flow)",
                showlegend=True,
                customdata=d["hover"],
                hovertemplate="%{customdata}<extra></extra>",
                width=0.28,
                opacity=0.95,
            ))
        else:
            fig.add_trace(go.Bar(
                x=[None],
                y=[None],
                orientation="h",
                marker_color=FLOW_KIND_COLOR[kind],
                marker_pattern_shape="/" if kind == "blocked" else "\\",
                name=f"{kind.title()} (flow)",
                showlegend=True,
            ))
    fig.update_layout(
        title="Runtime Timeline",
        barmode="overlay",
        xaxis=dict(type="date", range=[start_local, end_local], title="Time"),
        yaxis=dict(autorange="reversed", title="Station / EM", automargin=True),
        legend=dict(orientation="h", y=1.08, font_size=11),
        margin=dict(l=260, r=10, t=70, b=20),
        height=max(280, max(1, len(labels)) * 44 + 110),
    )

    ts_local = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(tz)
    df["Timestamp"] = (
        ts_local.dt.strftime("%Y-%m-%d %I:%M:%S.%f").str[:-3]
        + " " + ts_local.dt.strftime("%p")
    )
    df["Station"] = df["station"]
    df["Name"] = df["display_name"]
    df["EM"] = df["em_label"]
    df["From"] = df["from_state"].fillna("—")
    df["To"] = df["to_state"]
    df["Seq"] = df["active_seq"].fillna("").astype(str)
    df["Prod Seq"] = df["active_is_production"].map({True: "yes", False: "no"}).fillna("")
    df["Step"] = df["step_name"].fillna("")
    df["Description"] = df["step_desc"].fillna("")
    df["Automatic"] = df["automatic"].map({True: "T", False: "F"}).fillna("")
    df["Running"] = df["running"].map({True: "T", False: "F"}).fillna("")
    df["Paused"] = df["paused"].map({True: "T", False: "F"}).fillna("")
    df["Stopped"] = df["stopped"].map({True: "T", False: "F"}).fillna("")
    df["Unknown"] = df["unknown_status"].map({True: "T", False: "F"}).fillna("")
    df["Fault"] = df["fault"].map({True: "T", False: "F"}).fillna("")

    cols = [
        "Timestamp", "Station", "Name", "EM",
        "From", "To", "Seq", "Prod Seq",
        "Automatic", "Running", "Paused", "Stopped", "Unknown", "Fault",
        "Step", "Description",
    ]
    disp = df[cols] if not df.empty else pd.DataFrame(columns=cols)
    flow_table = html.Div("No blocked/starved events in selected range.", className="text-muted")
    if not flow_df.empty:
        flow_ts = pd.to_datetime(flow_df["start_ts"], utc=True, errors="coerce").dt.tz_convert(tz)
        flow_df["Start"] = (
            flow_ts.dt.strftime("%Y-%m-%d %I:%M:%S.%f").str[:-3]
            + " " + flow_ts.dt.strftime("%p")
        )
        flow_end = pd.to_datetime(flow_df["end_ts"], utc=True, errors="coerce")
        flow_end_local = flow_end.dt.tz_convert(tz)
        flow_df["End"] = flow_end_local.dt.strftime("%Y-%m-%d %I:%M:%S.%f").str[:-3]
        flow_df.loc[flow_df["end_ts"].isna(), "End"] = "ongoing"
        flow_df["Kind"] = flow_df["kind"].fillna("").str.title()
        flow_df["Reason"] = flow_df["reason_desc"].fillna("")
        flow_df["Station"] = flow_df["station"]
        flow_df["Name"] = flow_df["display_name"]
        flow_df["EM"] = flow_df["em_label"]
        flow_df["Seq"] = flow_df["seq_index"].fillna("").astype(str)
        flow_df["Step"] = flow_df["step_name"].fillna("")
        flow_df["Duration (s)"] = (
            pd.to_numeric(flow_df["duration_ms"], errors="coerce") / 1000.0
        ).round(3)
        flow_cols = ["Start", "End", "Duration (s)", "Kind", "Station", "Name", "EM", "Seq", "Step", "Reason"]
        flow_disp = flow_df[flow_cols]
        flow_table = dash_table.DataTable(
            data=flow_disp.to_dict("records"),
            columns=[{"name": c, "id": c} for c in flow_cols],
            sort_action="native",
            filter_action="native",
            page_size=15,
            style_table=DT_STYLE_TABLE,
            style_cell={**DT_STYLE_CELL, "whiteSpace": "normal", "height": "auto"},
            style_header=DT_STYLE_HEADER,
            style_filter=DT_STYLE_FILTER,
        )
    return html.Div([
        html.H6("Runtime Timeline", className="mb-2"),
        dcc.Graph(figure=fig, config={"displayModeBar": False}),
        html.H6("Runtime Transition Log", className="mt-3 mb-2"),
        html.Small(f"{len(disp):,} transition rows", className="text-muted d-block mb-2"),
        dash_table.DataTable(
            data=disp.to_dict("records"),
            columns=[{"name": c, "id": c} for c in cols],
            sort_action="native",
            filter_action="native",
            page_size=25,
            style_table=DT_STYLE_TABLE,
            style_cell={**DT_STYLE_CELL, "whiteSpace": "normal", "height": "auto"},
            style_header=DT_STYLE_HEADER,
            style_filter=DT_STYLE_FILTER,
        ),
        html.H6("Blocked / Starved Events", className="mt-4 mb-2"),
        flow_table,
    ])
