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
