"""
Live Status dashboard — one card per station showing current SEMI E10 state
and active step for each EM.

Cards auto-refresh every 5 s via the status-interval component.
Hover over the scrolling step text to pause it.
"""
from __future__ import annotations

import datetime
from collections import defaultdict

from dash import html

import db.queries as q

# ── Display config ────────────────────────────────────────────────────────────

STATE_LABEL = {
    "productive":        "Productive",
    "standby":           "Standby",
    "unscheduled_down":  "Fault",
    "manual":            "Manual",
    "unknown":           "Unknown",
}


def _state_badge(state: str) -> html.Span:
    label = STATE_LABEL.get(state, state.replace("_", " ").title())
    css   = f"state-badge state-{state}"
    return html.Span(
        [html.Span(className="state-dot"), label],
        className=css,
    )


def _marquee(step_name: str | None, step_desc: str | None) -> html.Div:
    if step_name and step_desc:
        text = f"{step_name}  —  {step_desc}"
    elif step_name:
        text = step_name
    else:
        text = "—"
    return html.Div(
        html.Span(text, className="step-marquee-inner"),
        className="step-marquee-outer",
        title=text,  # tooltip for accessibility
    )


def _em_row(em: dict) -> html.Div:
    seq_name  = em.get("seq_name") or ""
    header_children = [
        html.Span(em["em_label"], className="em-label-pill"),
        _state_badge(em.get("state") or "unknown"),
    ]
    if seq_name:
        header_children.append(
            html.Span(seq_name, className="seq-name-tag")
        )
    return html.Div(
        [
            html.Div(header_children, className="em-header"),
            _marquee(em.get("step_name"), em.get("step_desc")),
        ],
        className="em-row",
    )


def _station_card(display_name: str, station: str, ems: list[dict]) -> html.Div:
    # main EM first, then robots sorted by label
    sorted_ems = sorted(
        ems,
        key=lambda r: (0 if r["em_label"] == "main" else 1, r["em_label"]),
    )
    return html.Div(
        [
            # Dark header
            html.Div(
                [
                    html.P(display_name, className="station-display-name"),
                    html.P(station,      className="station-code"),
                ],
                className="station-card-header",
            ),
            # EM rows
            html.Div(
                [_em_row(em) for em in sorted_ems],
                className="station-card-body",
            ),
        ],
        className="station-card",
    )


def _connection_banner(plc_name: str) -> html.Div:
    """Small status bar showing collector ↔ PLC connectivity."""
    try:
        hb_df = q.get_collector_status()
        row   = hb_df[hb_df["plc_name"] == plc_name]
    except Exception:
        row = None

    if row is None or (hasattr(row, "empty") and row.empty):
        return html.Div(
            [html.Span("○", className="conn-dot conn-dot-unknown"),
             f"  {plc_name}  —  collector not seen"],
            className="conn-banner conn-unknown",
        )

    r          = row.iloc[0]
    connected  = bool(r["connected"])
    node_count = int(r["node_count"] or 0)
    last_seen  = r["last_seen"]

    # Compute age — last_seen is timezone-aware from TimescaleDB
    now = datetime.datetime.now(datetime.timezone.utc)
    if hasattr(last_seen, "tzinfo") and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=datetime.timezone.utc)
    age_s = (now - last_seen).total_seconds()

    if age_s < 60:
        age_str = f"{int(age_s)}s ago"
    elif age_s < 3600:
        age_str = f"{int(age_s / 60)}m ago"
    else:
        age_str = f"{int(age_s / 3600)}h ago"

    # Treat as stale if last heartbeat > 30 s old (collector may have died)
    stale = age_s > 30

    if connected and not stale:
        css_mod  = "conn-ok"
        dot_css  = "conn-dot conn-dot-ok"
        label    = f"{plc_name}  —  connected  ·  {node_count} nodes"
    elif connected and stale:
        css_mod  = "conn-stale"
        dot_css  = "conn-dot conn-dot-stale"
        label    = f"{plc_name}  —  stale  ·  last seen {age_str}"
    else:
        css_mod  = "conn-down"
        dot_css  = "conn-dot conn-dot-down"
        label    = f"{plc_name}  —  disconnected  ·  last seen {age_str}"

    return html.Div(
        [html.Span(className=dot_css), label],
        className=f"conn-banner {css_mod}",
    )


def render(plc_name: str) -> html.Div:
    if not plc_name:
        return html.Div("No PLC selected.", className="text-muted p-3")

    try:
        rows = q.query_station_status(plc_name)
    except Exception:
        return html.Div("Could not load station data.", className="text-muted p-3")

    if not rows:
        return html.Div(
            f"No stations found for {plc_name}.",
            className="text-muted p-3",
        )

    # Group by station, preserve station order from the query
    station_order: list[str] = []
    groups: dict[str, list[dict]] = defaultdict(list)
    names:  dict[str, str]        = {}

    for row in rows:
        key = row["station"]
        if key not in groups:
            station_order.append(key)
        groups[key].append(row)
        names[key] = row["display_name"]

    cards = [
        _station_card(names[s], s, groups[s])
        for s in station_order
    ]

    return html.Div([
        _connection_banner(plc_name),
        html.Div(cards, className="status-grid"),
    ])
