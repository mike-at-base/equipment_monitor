"""
Base Power brand tokens — dark theme.
Register the Plotly template once at app startup (imported by app/main.py).
"""
from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio

# ── Named brand colors ────────────────────────────────────────────────────────
LIVEWIRE  = "#b2dd79"   # only CTA / interactive accent
GROUNDED  = "#1e4d2b"   # navbar
RED       = "#c51808"   # semantic fault/error
CONDUIT   = "#f0eeeb"   # warm limestone — primary text on dark

# ── Dark mode surfaces ────────────────────────────────────────────────────────
BG        = "#1a1917"   # page background
SURFACE   = "#242220"   # cards, sidebar
ELEVATED  = "#2e2b29"   # table headers, inputs, dropdowns
BORDER    = "#3a3733"   # borders & dividers
MUTED     = "#9a9794"   # secondary / muted text

# Chart colorway — Livewire leads, then green variant, then blues/oranges
COLORWAY = [LIVEWIRE, "#5db87e", "#048ee5", "#ed6c30", RED, MUTED]

# ── Time formats — 12-hour clock used throughout the app ─────────────────────
# Python strftime (Windows-safe; %I always pads to 2 digits, which reads cleanly
# in tables alongside the date).  Use these constants instead of inline format
# strings so the whole app stays consistent.
TIME_FMT_TABLE = "%Y-%m-%d %I:%M:%S %p"   # full table cell:  2026-05-23 02:30:45 PM
TIME_FMT_SHORT = "%I:%M:%S %p"            # compact cell:     02:30:45 PM
TIME_FMT_MIN   = "%I:%M %p"               # to-the-minute:    02:30 PM

# Plotly tickformatstops — d3 format strings.  Applied globally via the
# base_power template so every date axis switches automatically between
# sub-second, time, and date labels using a 12-hour clock at the time levels.
PLOTLY_TICKFORMATSTOPS = [
    dict(dtickrange=[None, 1000],        value="%I:%M:%S.%L %p"),
    dict(dtickrange=[1000, 60000],       value="%I:%M:%S %p"),
    dict(dtickrange=[60000, 3600000],    value="%I:%M %p"),
    dict(dtickrange=[3600000, 86400000], value="%I %p"),
    dict(dtickrange=[86400000, "M1"],    value="%b %-d"),
    dict(dtickrange=["M1", "M12"],       value="%b '%y"),
    dict(dtickrange=["M12", None],       value="%Y"),
]
PLOTLY_HOVERFORMAT = "%Y-%m-%d %I:%M:%S %p"

# ── Plotly dark template ──────────────────────────────────────────────────────
pio.templates["base_power"] = go.layout.Template(
    layout=go.Layout(
        paper_bgcolor=BG,
        plot_bgcolor=SURFACE,
        font=dict(family="Inter, system-ui, sans-serif", color=CONDUIT, size=12),
        colorway=COLORWAY,
        xaxis=dict(
            gridcolor=BORDER, linecolor=BORDER,
            zerolinecolor=BORDER, tickcolor=BORDER,
            tickfont_color=MUTED,
            # 12-hour clock for any date-typed axis (ignored on non-date axes)
            tickformatstops=PLOTLY_TICKFORMATSTOPS,
            hoverformat=PLOTLY_HOVERFORMAT,
        ),
        yaxis=dict(
            gridcolor=BORDER, linecolor=BORDER,
            zerolinecolor=BORDER, tickcolor=BORDER,
            tickfont_color=MUTED,
        ),
        legend=dict(bgcolor="rgba(0,0,0,0)", font_color=CONDUIT),
        hoverlabel=dict(bgcolor=ELEVATED, bordercolor=BORDER, font_color=CONDUIT),
        title=dict(font=dict(color=CONDUIT, size=13, family="Inter, system-ui")),
    )
)
pio.templates.default = "base_power"

# ── DataTable shared styles ───────────────────────────────────────────────────
DT_STYLE_TABLE  = {"overflowX": "auto"}
DT_STYLE_HEADER = {
    "backgroundColor": ELEVATED,
    "color": MUTED,
    "fontWeight": "600",
    "fontSize": "11px",
    "textTransform": "uppercase",
    "letterSpacing": "0.07em",
    "borderColor": BORDER,
}
DT_STYLE_CELL = {
    "backgroundColor": SURFACE,
    "color": CONDUIT,
    "borderColor": BORDER,
    "fontSize": "13px",
    "padding": "5px 8px",
    "textAlign": "left",
}
DT_STYLE_FILTER = {
    "backgroundColor": ELEVATED,
    "color": CONDUIT,
    "borderColor": BORDER,
}

# Faulted row — clearly visible red tint on dark surface
FAULTED_COND = {
    "if": {"filter_query": '{Faulted} eq "yes"'},
    "backgroundColor": "rgba(197, 24, 8, 0.45)",
    "color": "#ffccc7",
}

# Availability % colour rules
AVAIL_PCT_COND = [
    {
        "if": {"filter_query": "{Availability %} >= 90", "column_id": "Availability %"},
        "color": LIVEWIRE, "fontWeight": "bold",
    },
    {
        "if": {"filter_query": "{Availability %} < 75", "column_id": "Availability %"},
        "color": RED, "fontWeight": "bold",
    },
]
