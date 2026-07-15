"""Plot styling shared by gallery notebooks."""

from __future__ import annotations

# Colorblind-safe categorical palette (Okabe-Ito).
PALETTE = [
    "#0072B2",
    "#E69F00",
    "#009E73",
    "#CC79A7",
    "#56B4E9",
    "#D55E00",
    "#F0E442",
    "#999999",
]


def altair_theme() -> None:
    """Register and enable the gallery altair theme. Transparent background so
    charts inherit marimo's own light/dark app theme."""
    import altair as alt

    @alt.theme.register("gallery", enable=True)
    def _gallery() -> alt.theme.ThemeConfig:
        return {
            "config": {
                "background": "transparent",
                "range": {"category": PALETTE},
                "axis": {"grid": True, "gridOpacity": 0.25, "domainOpacity": 0.5},
                "view": {"stroke": None},
                "legend": {"orient": "bottom"},
            }
        }
