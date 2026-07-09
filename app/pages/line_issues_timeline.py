from __future__ import annotations

import datetime
import os
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
from dash import dcc, html

import db.queries as q
from app.cache import ttl_get_or_set

AVAIL_COLOR = {
    "productive": "#b2dd79",
    "standby": "#f7c33c",
    "unscheduled_down": "#c51808",
    "manual": "#3a3733",
}
AVAIL_ORDER = ["productive", "standby", "unscheduled_down", "manual"]
FLOW_COLOR = {"blocked": "#6a5acd", "starved": "#00a3c4"}
FLOW_ORDER = ["blocked", "starved"]


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


def _enabled_main_ems() -> pd.DataFrame:
    rows: list[dict] = []
    for p in q.get_all_plcs():
        if not p.get("enabled", True):
            continue
        plc = p["name"]
        for e in q.get_enabled_ems(plc):
            if str(e.get("em_label", "")).lower() != "main":
                continue
            rows.append({
                "em_id": int(e["id"]),
                "plc_name": plc,
                "station": str(e.get("station") or ""),
                "display_name": str(e.get("display_name") or ""),
            })
    return pd.DataFrame(rows)


def render(
    start: datetime.datetime,
    end: datetime.datetime,
    included_plcs: list[str] | None = None,
    excluded_station_keys: list[str] | None = None,
) -> html.Div:
    meta = ttl_get_or_set(("line_issues", "main_meta"), 120, _enabled_main_ems)
    if meta.empty:
        return html.Div("No enabled main stations across PLCs.", className="text-muted")

    plc_set = {str(p) for p in (included_plcs or []) if str(p).strip()}
    if plc_set:
        meta = meta[meta["plc_name"].isin(plc_set)].copy()
    excluded = {str(k) for k in (excluded_station_keys or []) if str(k).strip()}
    if excluded:
        keys = meta["plc_name"].astype(str) + "|" + meta["station"].astype(str)
        meta = meta[~keys.isin(excluded)].copy()
    if meta.empty:
        return html.Div("No stations left after line timeline filters.", className="text-muted")

    em_ids = meta["em_id"].astype(int).tolist()
    em_key = tuple(sorted(em_ids))
    tl = ttl_get_or_set(
        ("line_issues", "tl", em_key, start.isoformat(), end.isoformat()),
        20,
        lambda: q.query_state_timeline(em_ids, start, end),
    )
    flow = ttl_get_or_set(
        ("line_issues", "flow", em_key, start.isoformat(), end.isoformat()),
        20,
        lambda: q.query_flow_events(em_ids, start, end, limit=30000),
    )
    if tl.empty and flow.empty:
        return html.Div("No availability or blocked/starved data in selected window.", className="text-muted")

    tz = _app_tz()
    start_local = _to_plant_time(start, tz)
    end_local = _to_plant_time(end, tz)
    labels = (
        meta.sort_values(["plc_name", "station"])
        .assign(y=lambda d: d["plc_name"] + " / " + d["station"] + " — " + d["display_name"])
    )
    y_map = {int(r["em_id"]): r["y"] for _, r in labels.iterrows()}
    id_to_plc_station = {
        int(r["em_id"]): (str(r["plc_name"]), str(r["station"]), str(r["display_name"]))
        for _, r in labels.iterrows()
    }

    avail_buckets: dict[str, dict] = {s: {"x": [], "y": [], "base": [], "hover": [], "cd": []} for s in AVAIL_ORDER}
    if not tl.empty:
        tl = tl.copy()
        tl["ts"] = pd.to_datetime(tl["ts"], utc=True, errors="coerce")
        tl["next_ts"] = pd.to_datetime(tl["next_ts"], utc=True, errors="coerce")
        for _, r in tl.iterrows():
            em_id = int(r["em_id"])
            if em_id not in y_map:
                continue
            t0 = r["ts"]
            t1 = r["next_ts"]
            if pd.isna(t0) or pd.isna(t1) or t1 <= t0:
                continue
            state = str(r.get("state") or "manual")
            if state not in avail_buckets:
                state = "manual"
            plc, station, disp = id_to_plc_station[em_id]
            t0l = _to_plant_time(t0, tz)
            t1l = _to_plant_time(t1, tz)
            b = avail_buckets[state]
            b["x"].append((t1 - t0).total_seconds() * 1000.0)
            b["y"].append(y_map[em_id])
            b["base"].append(t0.timestamp() * 1000.0)
            b["hover"].append(
                f"{state.replace('_',' ').title()}<br>{plc} / {station} — {disp}<br>"
                f"{t0l.strftime('%Y-%m-%d %I:%M:%S %p')} - {t1l.strftime('%Y-%m-%d %I:%M:%S %p')}"
            )
            b["cd"].append([station, plc])

    flow_buckets: dict[str, dict] = {k: {"x": [], "y": [], "base": [], "hover": [], "cd": []} for k in FLOW_ORDER}
    if not flow.empty:
        flow = flow.copy()
        flow["start_ts"] = pd.to_datetime(flow["start_ts"], utc=True, errors="coerce")
        flow["end_ts"] = pd.to_datetime(flow["end_ts"], utc=True, errors="coerce")
        end_utc = pd.Timestamp(end)
        if end_utc.tzinfo is None:
            end_utc = end_utc.tz_localize("UTC")
        else:
            end_utc = end_utc.tz_convert("UTC")
        for _, r in flow.iterrows():
            em_id = int(r["em_id"])
            if em_id not in y_map:
                continue
            kind = str(r.get("kind") or "").lower()
            if kind not in flow_buckets:
                continue
            t0 = r["start_ts"]
            t1 = r["end_ts"] if pd.notna(r["end_ts"]) else end_utc
            if pd.isna(t0) or pd.isna(t1) or t1 <= t0:
                continue
            plc, station, disp = id_to_plc_station[em_id]
            reason = str(r.get("reason_desc") or "").strip()
            step = str(r.get("step_name") or "").strip()
            extra = []
            if step:
                extra.append(f"Step: {step}")
            if reason:
                extra.append(f"Reason: {reason}")
            extra_txt = ("<br>" + "<br>".join(extra)) if extra else ""
            t0l = _to_plant_time(t0, tz)
            t1l = _to_plant_time(t1, tz)
            b = flow_buckets[kind]
            b["x"].append((t1 - t0).total_seconds() * 1000.0)
            b["y"].append(y_map[em_id])
            b["base"].append(t0.timestamp() * 1000.0)
            b["hover"].append(
                f"{kind.title()}<br>{plc} / {station} — {disp}<br>"
                f"{t0l.strftime('%Y-%m-%d %I:%M:%S %p')} - {t1l.strftime('%Y-%m-%d %I:%M:%S %p')}{extra_txt}"
            )
            b["cd"].append([station, plc])

    fig = go.Figure()
    for s in AVAIL_ORDER:
        d = avail_buckets[s]
        if d["x"]:
            fig.add_trace(go.Bar(
                x=d["x"], y=d["y"], orientation="h", base=d["base"],
                marker_color=AVAIL_COLOR[s], name=s.replace("_", " ").title(),
                customdata=d["cd"], hovertemplate="%{text}<extra></extra>", text=d["hover"],
                width=0.62,
            ))
        else:
            fig.add_trace(go.Bar(x=[None], y=[None], orientation="h", marker_color=AVAIL_COLOR[s], name=s.replace("_", " ").title()))
    for k in FLOW_ORDER:
        d = flow_buckets[k]
        shape = "/" if k == "blocked" else "\\"
        if d["x"]:
            fig.add_trace(go.Bar(
                x=d["x"], y=d["y"], orientation="h", base=d["base"],
                marker_color=FLOW_COLOR[k], marker_pattern_shape=shape,
                name=f"{k.title()} (flow)", customdata=d["cd"],
                hovertemplate="%{text}<extra></extra>", text=d["hover"],
                width=0.28, opacity=0.95,
            ))
        else:
            fig.add_trace(go.Bar(x=[None], y=[None], orientation="h", marker_color=FLOW_COLOR[k], marker_pattern_shape=shape, name=f"{k.title()} (flow)"))

    fig.update_layout(
        title="Line Issues Timeline — All PLC Stations",
        barmode="overlay",
        xaxis=dict(type="date", range=[start_local, end_local], title="Time"),
        yaxis=dict(autorange="reversed", title="PLC / Station", automargin=True),
        legend=dict(orientation="h", y=1.08, font_size=11),
        margin=dict(l=320, r=10, t=70, b=20),
        height=max(360, 34 * max(1, len(labels)) + 120),
    )

    return html.Div([
        html.Div(
            f"Window: {start_local.strftime('%Y-%m-%d %I:%M %p')} - {end_local.strftime('%Y-%m-%d %I:%M %p')} ({os.environ.get('APP_TIMEZONE', 'America/Chicago')})",
            className="text-muted small mb-2",
        ),
        html.Div("Availability states are the base bars; blocked/starved are overlaid patterned bars.", className="text-muted small mb-2"),
        dcc.Graph(id="line-issues-timeline-chart", figure=fig, config={"displayModeBar": False}),
    ])
