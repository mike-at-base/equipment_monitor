from __future__ import annotations

import datetime
import os
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
from dash import dash_table, dcc, html

import db.queries as q
from app.cache import ttl_get_or_set
from app.brand import DT_STYLE_CELL, DT_STYLE_FILTER, DT_STYLE_HEADER, DT_STYLE_TABLE


def _app_tz() -> datetime.tzinfo:
    tz_name = os.environ.get("APP_TIMEZONE", "America/Chicago")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return datetime.timezone.utc


def _today_window_utc() -> tuple[datetime.datetime, datetime.datetime, datetime.datetime]:
    tz = _app_tz()
    now_local = datetime.datetime.now(tz)
    start_local = now_local.replace(hour=7, minute=0, second=0, microsecond=0)
    shift_end_local = now_local.replace(hour=15, minute=30, second=0, microsecond=0)
    end_local = min(now_local, shift_end_local)
    if end_local < start_local:
        end_local = start_local
    return (
        start_local.astimezone(datetime.timezone.utc),
        end_local.astimezone(datetime.timezone.utc),
        now_local,
    )


def _station_availability(summary_df: pd.DataFrame) -> pd.DataFrame:
    if summary_df.empty:
        return pd.DataFrame(columns=["station", "display_name", "availability_pct"])
    s = summary_df.copy()
    for col in ("productive_min", "standby_min", "down_min", "manual_min"):
        s[col] = pd.to_numeric(s[col], errors="coerce").fillna(0.0)
    g = (
        s.groupby("station", as_index=False)
        .agg(
            display_name=("display_name", "first"),
            productive_min=("productive_min", "sum"),
            standby_min=("standby_min", "sum"),
            down_min=("down_min", "sum"),
            manual_min=("manual_min", "sum"),
        )
    )
    sched = g["productive_min"] + g["standby_min"] + g["down_min"]
    g["availability_pct"] = ((g["productive_min"] + g["standby_min"]) / sched * 100.0).where(sched > 0)
    return g


def _station_flow_today(
    flow_df: pd.DataFrame,
    start_utc: datetime.datetime,
    end_utc: datetime.datetime,
) -> pd.DataFrame:
    if flow_df.empty:
        return pd.DataFrame(columns=["station", "display_name", "blocked_min", "starved_min", "flow_loss_min", "events"])

    d = flow_df.copy()
    d["start_ts"] = pd.to_datetime(d["start_ts"], utc=True, errors="coerce")
    d["end_ts"] = pd.to_datetime(d["end_ts"], utc=True, errors="coerce")
    d["kind"] = d["kind"].fillna("").astype(str).str.lower()
    end_ts = pd.Timestamp(end_utc)
    start_ts = pd.Timestamp(start_utc)
    d["seg_start"] = d["start_ts"].clip(lower=start_ts)
    d["seg_end"] = d["end_ts"].fillna(end_ts).clip(upper=end_ts)
    d["overlap_ms"] = (d["seg_end"] - d["seg_start"]).dt.total_seconds() * 1000.0
    d = d[d["overlap_ms"] > 0]
    if d.empty:
        return pd.DataFrame(columns=["station", "display_name", "blocked_min", "starved_min", "flow_loss_min", "events"])

    p = (
        d.pivot_table(
            index=["station", "display_name"],
            columns="kind",
            values="overlap_ms",
            aggfunc="sum",
            fill_value=0.0,
        )
        .reset_index()
    )
    if "blocked" not in p.columns:
        p["blocked"] = 0.0
    if "starved" not in p.columns:
        p["starved"] = 0.0
    events = d.groupby(["station", "display_name"], as_index=False).size().rename(columns={"size": "events"})
    out = p.merge(events, on=["station", "display_name"], how="left")
    out["blocked_min"] = pd.to_numeric(out["blocked"], errors="coerce").fillna(0.0) / 60000.0
    out["starved_min"] = pd.to_numeric(out["starved"], errors="coerce").fillna(0.0) / 60000.0
    out["flow_loss_min"] = out["blocked_min"] + out["starved_min"]
    return out[["station", "display_name", "blocked_min", "starved_min", "flow_loss_min", "events"]]


def render(plc_name: str, excluded_stations: list[str] | None = None) -> html.Div:
    start_utc, end_utc, now_local = _today_window_utc()
    rows = ttl_get_or_set(
        ("mod_bottleneck", "status", plc_name),
        20,
        lambda: q.query_station_status(plc_name),
    )
    if not rows:
        return html.Div("No station data for selected PLC.", className="text-muted")
    em_ids = sorted({int(r["em_id"]) for r in rows if r.get("em_id") is not None})
    if not em_ids:
        return html.Div("No enabled equipment modules for selected PLC.", className="text-muted")

    em_key = tuple(sorted(em_ids))
    avail_raw = ttl_get_or_set(
        ("mod_bottleneck", "avail_raw", plc_name, em_key, start_utc.isoformat(), end_utc.isoformat()),
        30,
        lambda: q.query_state_summary(em_ids, start_utc, end_utc),
    )
    flow_raw = ttl_get_or_set(
        ("mod_bottleneck", "flow_raw", plc_name, em_key, start_utc.isoformat(), end_utc.isoformat()),
        30,
        lambda: q.query_flow_events(em_ids, start_utc, end_utc, limit=10000),
    )
    avail_df = _station_availability(avail_raw)
    flow_df = _station_flow_today(flow_raw, start_utc, end_utc)
    df = avail_df.merge(flow_df, on=["station", "display_name"], how="left")
    for c in ("blocked_min", "starved_min", "flow_loss_min", "events"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    excluded = {str(s) for s in (excluded_stations or []) if str(s).strip()}
    if excluded:
        df = df[~df["station"].astype(str).isin(excluded)].copy()
    if df.empty:
        return html.Div("No stations left after exclusions for today.", className="text-muted")

    # Constraint-style bottleneck: least blocked/starved among stations that
    # actually did work today. If none have productive time, fall back to all.
    ranking_pool = df[df["productive_min"] > 0].copy()
    if ranking_pool.empty:
        ranking_pool = df.copy()
    ranking_pool = ranking_pool.sort_values(
        ["flow_loss_min", "blocked_min", "starved_min", "availability_pct", "productive_min"],
        ascending=[True, True, True, False, False],
    ).reset_index(drop=True)
    bottleneck = ranking_pool.iloc[0]
    df = ranking_pool
    bottleneck_avail = "—" if pd.isna(bottleneck["availability_pct"]) else f"{float(bottleneck['availability_pct']):.1f}%"

    disp = df.copy()
    disp["Station"] = disp["station"]
    disp["Name"] = disp["display_name"]
    disp["Availability %"] = disp["availability_pct"].map(lambda v: "—" if pd.isna(v) else f"{float(v):.1f}")
    disp["Blocked (min)"] = disp["blocked_min"].map(lambda v: f"{float(v):.1f}")
    disp["Starved (min)"] = disp["starved_min"].map(lambda v: f"{float(v):.1f}")
    disp["Flow Loss (min)"] = disp["flow_loss_min"].map(lambda v: f"{float(v):.1f}")
    disp["Events"] = disp["events"].astype(int)
    table_cols = ["Station", "Name", "Availability %", "Blocked (min)", "Starved (min)", "Flow Loss (min)", "Events"]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=disp["Station"],
        y=df["blocked_min"],
        name="Blocked (min)",
        marker_color="#6a5acd",
        customdata=disp[["Station", "Name"]].to_numpy(),
        hovertemplate="%{customdata[0]} — %{customdata[1]}<br>Blocked %{y:.1f} min<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=disp["Station"],
        y=df["starved_min"],
        name="Starved (min)",
        marker_color="#00a3c4",
        customdata=disp[["Station", "Name"]].to_numpy(),
        hovertemplate="%{customdata[0]} — %{customdata[1]}<br>Starved %{y:.1f} min<extra></extra>",
    ))
    fig.update_layout(
        barmode="stack",
        title=f"{plc_name} Constraint Candidate Today (Least Flow Loss)",
        xaxis_title="Station",
        yaxis_title="Flow loss (minutes)",
        legend=dict(orientation="h", y=1.08),
        margin=dict(l=60, r=10, t=55, b=55),
        height=360,
    )

    return html.Div([
        html.Div(
            f"Today ({now_local.strftime('%Y-%m-%d')}) shift window 07:00-15:30 — {plc_name}",
            className="text-muted small mb-1",
        ),
        html.Div(
            [
                html.Span("Bottleneck today: ", className="text-muted"),
                html.Strong(f"{bottleneck['station']}"),
                html.Span(f" ({bottleneck['display_name']}) · ", className="text-muted"),
                html.Span(f"Least flow loss {float(bottleneck['flow_loss_min']):.1f} min · ", className="text-muted"),
                html.Span(f"Availability {bottleneck_avail}", className="text-muted"),
            ],
            className="mb-3",
        ),
        dcc.Graph(id="mod-bottleneck-chart", figure=fig, config={"displayModeBar": False}),
        html.H6("Station Flow Loss & Availability (Today)", className="mt-3 mb-2"),
        html.Div("Click a row or bar to open station Runtime details.", className="text-muted small mb-2"),
        dash_table.DataTable(
            id="mod-bottleneck-table",
            data=disp[table_cols].to_dict("records"),
            columns=[{"name": c, "id": c} for c in table_cols],
            sort_action="native",
            filter_action="native",
            page_size=18,
            style_table=DT_STYLE_TABLE,
            style_cell=DT_STYLE_CELL,
            style_header=DT_STYLE_HEADER,
            style_filter=DT_STYLE_FILTER,
        ),
    ])
