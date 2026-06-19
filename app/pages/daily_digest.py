"""
Daily digest view:
  - Line-wide fault summary
  - Top faults per station
  - Shift-over-shift anomaly flags
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass

import dash_bootstrap_components as dbc
import pandas as pd
from dash import dash_table, html

import db.queries as q
from app.brand import DT_STYLE_CELL, DT_STYLE_FILTER, DT_STYLE_HEADER, DT_STYLE_TABLE


@dataclass
class DigestData:
    line_faults: pd.DataFrame
    station_faults: pd.DataFrame
    anomalies: pd.DataFrame
    summary_cards: dict[str, str]


def _issue_label(df: pd.DataFrame) -> pd.Series:
    step = df.get("step_name", pd.Series("", index=df.index)).fillna("").astype(str).str.strip()
    desc = df.get("step_desc", pd.Series("", index=df.index)).fillna("").astype(str).str.strip()
    ext = df.get("ext_fault_msg", pd.Series("", index=df.index)).fillna("").astype(str).str.strip()
    out = step.copy()
    has_desc = desc != ""
    has_ext = (~has_desc) & (ext != "")
    out.loc[has_desc] = step.loc[has_desc] + " — " + desc.loc[has_desc]
    out.loc[has_ext] = step.loc[has_ext] + " — " + ext.loc[has_ext]
    out = out.replace("", "(unknown fault)")
    return out


def _fmt(v: float | int | None, digits: int = 1) -> str:
    if v is None or pd.isna(v):
        return "0"
    if isinstance(v, int):
        return f"{v:,}"
    return f"{float(v):,.{digits}f}"


def _build_digest(plc_name: str, shift_hours: int, now_utc: datetime.datetime) -> DigestData:
    rows = q.query_station_status(plc_name)
    em_ids = sorted({int(r["em_id"]) for r in rows if r.get("em_id") is not None})
    if not em_ids:
        empty = pd.DataFrame()
        return DigestData(empty, empty, empty, {
            "line_availability": "—",
            "fault_events": "0",
            "faulted_minutes": "0",
            "stations_flagged": "0",
        })

    end = now_utc
    start = end - datetime.timedelta(hours=shift_hours)
    prev_start = start - datetime.timedelta(hours=shift_hours)
    prev_end = start

    fault_df = q.query_fault_events(em_ids, None, start, end)
    state_df = q.query_state_summary(em_ids, start, end)
    prev_state_df = q.query_state_summary(em_ids, prev_start, prev_end)
    prev_fault_df = q.query_fault_events(em_ids, None, prev_start, prev_end)

    # ── Line-wide top faults ────────────────────────────────────────────────
    if fault_df.empty:
        line_faults = pd.DataFrame(columns=["Fault", "Events", "Total Duration (min)"])
        station_faults = pd.DataFrame(
            columns=["Station", "Fault", "Events", "Total Duration (min)"]
        )
    else:
        f = fault_df.copy()
        f["duration_ms"] = pd.to_numeric(f["duration_ms"], errors="coerce").fillna(0.0)
        f["issue"] = _issue_label(f)

        line_faults = (
            f.groupby("issue", as_index=False)
            .agg(events=("issue", "count"), total_ms=("duration_ms", "sum"))
            .sort_values(["events", "total_ms"], ascending=[False, False])
            .head(15)
        )
        line_faults["Fault"] = line_faults["issue"]
        line_faults["Events"] = line_faults["events"].astype(int)
        line_faults["Total Duration (min)"] = (line_faults["total_ms"] / 60000.0).round(1)
        line_faults = line_faults[["Fault", "Events", "Total Duration (min)"]]

        station_faults = (
            f.groupby(["station", "issue"], as_index=False)
            .agg(events=("issue", "count"), total_ms=("duration_ms", "sum"))
            .sort_values(["station", "events", "total_ms"], ascending=[True, False, False])
        )
        station_faults["rank"] = station_faults.groupby("station").cumcount() + 1
        station_faults = station_faults[station_faults["rank"] <= 5].copy()
        station_faults["Station"] = station_faults["station"]
        station_faults["Fault"] = station_faults["issue"]
        station_faults["Events"] = station_faults["events"].astype(int)
        station_faults["Total Duration (min)"] = (station_faults["total_ms"] / 60000.0).round(1)
        station_faults = station_faults[["Station", "Fault", "Events", "Total Duration (min)"]]

    # ── Shift anomalies ─────────────────────────────────────────────────────
    cur = state_df.copy()
    prev = prev_state_df.copy()
    for df in (cur, prev):
        if df.empty:
            continue
        df["down_min"] = pd.to_numeric(df["down_min"], errors="coerce").fillna(0.0)
        df["availability_pct"] = pd.to_numeric(df["availability_pct"], errors="coerce")

    if cur.empty:
        anomalies = pd.DataFrame(columns=["Station", "Signal", "Current", "Previous", "Delta"])
        line_avail = None
        down_min_total = 0.0
    else:
        cur_station = (
            cur.groupby("station", as_index=False)
            .agg(
                current_avail=("availability_pct", "mean"),
                current_down=("down_min", "sum"),
            )
        )
        prev_station = (
            prev.groupby("station", as_index=False)
            .agg(
                prev_avail=("availability_pct", "mean"),
                prev_down=("down_min", "sum"),
            )
            if not prev.empty else pd.DataFrame(columns=["station", "prev_avail", "prev_down"])
        )

        cur_fault_ct = (
            fault_df.groupby("station").size().rename("current_faults").reset_index()
            if not fault_df.empty else pd.DataFrame(columns=["station", "current_faults"])
        )
        prev_fault_ct = (
            prev_fault_df.groupby("station").size().rename("prev_faults").reset_index()
            if not prev_fault_df.empty else pd.DataFrame(columns=["station", "prev_faults"])
        )

        cmp_df = (
            cur_station
            .merge(prev_station, on="station", how="left")
            .merge(cur_fault_ct, on="station", how="left")
            .merge(prev_fault_ct, on="station", how="left")
            .fillna({"prev_avail": 0.0, "prev_down": 0.0, "current_faults": 0, "prev_faults": 0})
        )
        cmp_df["avail_delta"] = cmp_df["current_avail"] - cmp_df["prev_avail"]
        cmp_df["down_delta"] = cmp_df["current_down"] - cmp_df["prev_down"]
        cmp_df["fault_delta"] = cmp_df["current_faults"] - cmp_df["prev_faults"]

        rows_out: list[dict] = []
        for _, r in cmp_df.iterrows():
            st = r["station"]
            if r["avail_delta"] <= -10:
                rows_out.append({
                    "Station": st, "Signal": "Availability drop",
                    "Current": f"{r['current_avail']:.1f}%", "Previous": f"{r['prev_avail']:.1f}%",
                    "Delta": f"{r['avail_delta']:.1f} pt",
                })
            if r["current_faults"] >= 5 and r["current_faults"] >= max(3, 2 * int(r["prev_faults"])):
                rows_out.append({
                    "Station": st, "Signal": "Fault count spike",
                    "Current": int(r["current_faults"]), "Previous": int(r["prev_faults"]),
                    "Delta": int(r["fault_delta"]),
                })
            if r["current_down"] >= 60 and r["down_delta"] >= 30:
                rows_out.append({
                    "Station": st, "Signal": "Down-time increase",
                    "Current": f"{r['current_down']:.1f} min", "Previous": f"{r['prev_down']:.1f} min",
                    "Delta": f"{r['down_delta']:.1f} min",
                })
        anomalies = pd.DataFrame(rows_out, columns=["Station", "Signal", "Current", "Previous", "Delta"])
        line_avail = cur_station["current_avail"].mean() if not cur_station.empty else None
        down_min_total = cur_station["current_down"].sum()

    summary_cards = {
        "line_availability": "—" if line_avail is None or pd.isna(line_avail) else f"{line_avail:.1f}%",
        "fault_events": _fmt(0 if fault_df.empty else len(fault_df), digits=0),
        "faulted_minutes": _fmt(down_min_total, digits=1),
        "stations_flagged": _fmt(0 if anomalies.empty else anomalies["Station"].nunique(), digits=0),
    }
    return DigestData(line_faults, station_faults, anomalies, summary_cards)


def _table(df: pd.DataFrame, table_id: str, page_size: int = 12) -> dash_table.DataTable:
    return dash_table.DataTable(
        id=table_id,
        data=df.to_dict("records"),
        columns=[{"name": c, "id": c} for c in df.columns],
        sort_action="native",
        filter_action="native",
        page_size=page_size,
        style_table=DT_STYLE_TABLE,
        style_cell={**DT_STYLE_CELL, "whiteSpace": "normal", "height": "auto"},
        style_header=DT_STYLE_HEADER,
        style_filter=DT_STYLE_FILTER,
    )


def render(plc_name: str, shift_hours: int, now_utc: datetime.datetime) -> html.Div:
    digest = _build_digest(plc_name, shift_hours, now_utc)

    cards = dbc.Row(
        [
            dbc.Col(dbc.Card(dbc.CardBody([html.Small("Line Availability"), html.H4(digest.summary_cards["line_availability"])]))),
            dbc.Col(dbc.Card(dbc.CardBody([html.Small("Fault Events"), html.H4(digest.summary_cards["fault_events"])]))),
            dbc.Col(dbc.Card(dbc.CardBody([html.Small("Faulted Minutes"), html.H4(digest.summary_cards["faulted_minutes"])]))),
            dbc.Col(dbc.Card(dbc.CardBody([html.Small("Stations Flagged"), html.H4(digest.summary_cards["stations_flagged"])]))),
        ],
        className="g-2 mb-3",
    )

    line_faults = (
        _table(digest.line_faults, "digest-line-faults-table", page_size=15)
        if not digest.line_faults.empty
        else html.Div("No faults in current shift window.", className="text-muted")
    )
    station_faults = (
        _table(digest.station_faults, "digest-station-faults-table", page_size=18)
        if not digest.station_faults.empty
        else html.Div("No station fault aggregates in current shift window.", className="text-muted")
    )
    anomalies = (
        _table(digest.anomalies, "digest-anomalies-table", page_size=12)
        if not digest.anomalies.empty
        else html.Div("No anomaly flags for the current shift.", className="text-muted")
    )

    return html.Div(
        [
            html.Div(
                f"Shift window: last {shift_hours}h (UTC ending {now_utc.strftime('%Y-%m-%d %H:%M')})",
                className="text-muted small mb-2",
            ),
            cards,
            html.H6("Line-Wide Top Faults", className="mb-2"),
            line_faults,
            html.H6("Top Faults Per Station", className="mt-4 mb-2"),
            station_faults,
            html.H6("Shift Anomaly Flags", className="mt-4 mb-2"),
            anomalies,
        ]
    )


def build_export_payload(plc_name: str, shift_hours: int, now_utc: datetime.datetime) -> dict:
    digest = _build_digest(plc_name, shift_hours, now_utc)
    return {
        "generated_at_utc": now_utc.isoformat(),
        "plc": plc_name,
        "shift_hours": shift_hours,
        "summary": digest.summary_cards,
        "line_top_faults": digest.line_faults.to_dict("records"),
        "station_top_faults": digest.station_faults.to_dict("records"),
        "anomalies": digest.anomalies.to_dict("records"),
    }
