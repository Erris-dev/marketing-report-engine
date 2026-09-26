"""Static report charts (matplotlib, rendered to SVG for a crisp PDF).

Colors follow the reference data-viz palette. Channel colors are fixed per channel
(never by rank) and validated for color-vision deficiency; the low-contrast slots get
direct labels, and the report's KPI table is the table view. There is never a second
y-axis: each chart answers one question.
"""

from __future__ import annotations

import io
from collections.abc import Sequence

import matplotlib

matplotlib.use("Agg")  # headless rendering, also inside Docker

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from mre.anomalies import Anomaly
from mre.metrics import TOTAL

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SERIES_BLUE = "#2a78d6"
DIVERGING_UP = "#2a78d6"
DIVERGING_DOWN = "#e34948"
CRITICAL = "#d03b3b"
HIGHLIGHT_BAND = "#f0efec"

# Fixed identity colors, in categorical slot order; unknown folds to neutral gray.
CHANNEL_ORDER = ("organic_search", "referral", "direct", "paid_search", "other", "unknown")
CHANNEL_COLORS = {
    "organic_search": "#2a78d6",
    "referral": "#eb6834",
    "direct": "#1baf7a",
    "paid_search": "#eda100",
    "other": "#e87ba4",
    "unknown": MUTED,
}
CHANNEL_LABELS = {
    "organic_search": "Organic search",
    "referral": "Referral",
    "direct": "Direct",
    "paid_search": "Paid search",
    "other": "Other",
    "unknown": "Unknown",
    TOTAL: "All channels",
}

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 8.5,
        "axes.edgecolor": AXIS,
        "axes.labelcolor": INK_SECONDARY,
        "axes.titlecolor": INK,
        "axes.titlesize": 10,
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "svg.fonttype": "none",  # keep text as text in the SVG
    }
)


def channel_label(channel: str) -> str:
    return CHANNEL_LABELS.get(channel, channel.replace("_", " ").capitalize())


def _figure(width: float = 7.0, height: float = 2.6) -> tuple[Figure, Axes]:
    fig, ax = plt.subplots(figsize=(width, height))
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    return fig, ax


def _to_svg(fig: Figure) -> str:
    buffer = io.StringIO()
    fig.savefig(buffer, format="svg", bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return buffer.getvalue()


def _money_axis(value: float, _pos: object) -> str:
    return f"${value / 1000:,.0f}k" if abs(value) >= 1000 else f"${value:,.0f}"


def revenue_trend(weekly: pd.DataFrame, report_week: str, anomalies: Sequence[Anomaly]) -> str:
    """Weekly total revenue over complete weeks, report week highlighted, anomaly markers."""
    total = weekly[(weekly["channel"] == TOTAL) & weekly["is_complete"]].sort_values("week_start")
    weeks = list(total["week"])
    x = range(len(weeks))
    fig, ax = _figure()
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)

    if report_week in weeks:
        i = weeks.index(report_week)
        ax.axvspan(i - 0.45, i + 0.45, color=HIGHLIGHT_BAND, zorder=0)
    ax.plot(x, total["revenue"], color=SERIES_BLUE, linewidth=2, zorder=2)

    high_weeks = {a.week for a in anomalies if a.severity == "high"}
    flagged = [i for i, w in enumerate(weeks) if w in high_weeks]
    if flagged:
        ax.scatter(
            flagged,
            total["revenue"].to_numpy()[flagged],
            s=46,
            color=CRITICAL,
            edgecolors=SURFACE,
            linewidths=2,  # surface ring so the marker separates from the line
            zorder=3,
            label="Week with a high-severity anomaly",
        )
        ax.legend(
            loc="upper right",
            bbox_to_anchor=(1.0, 1.13),
            frameon=False,
            fontsize=8,
            labelcolor=INK_SECONDARY,
            handletextpad=0.3,
        )

    ax.set_xticks(list(x), [w.replace("-W", "\nW") for w in weeks], fontsize=7)
    ax.yaxis.set_major_formatter(FuncFormatter(_money_axis))
    ax.set_ylim(bottom=0)
    ax.set_title("Weekly revenue, all channels")
    return _to_svg(fig)


def channel_mix(weekly: pd.DataFrame, week: str) -> str:
    """Share of sessions vs share of revenue by channel (100% stacked bars)."""
    rows = weekly[(weekly["week"] == week) & (weekly["channel"] != TOTAL)].set_index("channel")
    channels = [c for c in CHANNEL_ORDER if c in rows.index] + sorted(
        c for c in rows.index if c not in CHANNEL_ORDER
    )
    fig, ax = _figure(height=1.9)
    bars = (("Revenue", "revenue_share"), ("Sessions", "session_share"))
    for y, (_label, column) in enumerate(bars):
        left = 0.0
        for channel in channels:
            share = float(rows.loc[channel, column] or 0.0)
            if share <= 0:
                continue
            ax.barh(
                y,
                share,
                left=left,
                height=0.55,
                color=CHANNEL_COLORS.get(channel, MUTED),
                edgecolor=SURFACE,
                linewidth=2,  # 2px surface gap between segments
                label=channel_label(channel) if y == 0 else None,
            )
            if share >= 0.07:  # direct labels where they fit
                ax.text(
                    left + share / 2,
                    y,
                    f"{share:.0%}",
                    ha="center",
                    va="center",
                    fontsize=7.5,
                    color=INK,
                )
            left += share
    ax.set_yticks(range(len(bars)), [label for label, _ in bars], color=INK_SECONDARY)
    ax.set_xlim(0, 1)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:.0%}"))
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.28),
        ncol=len(channels),
        frameon=False,
        fontsize=7.5,
        labelcolor=INK_SECONDARY,
        handlelength=1,
        columnspacing=1.2,
    )
    ax.set_title("Channel mix")
    return _to_svg(fig)


FUNNEL_STEPS = (
    ("sessions", "All sessions"),
    ("view_item_sessions", "Viewed an item"),
    ("add_to_cart_sessions", "Added to cart"),
    ("begin_checkout_sessions", "Began checkout"),
    ("purchase_sessions", "Purchased"),
)


def funnel(weekly: pd.DataFrame, week: str) -> str:
    """Sessions reaching each funnel step (all channels)."""
    row = weekly[(weekly["week"] == week) & (weekly["channel"] == TOTAL)].iloc[0]
    values = [float(row[col]) for col, _ in FUNNEL_STEPS]
    labels = [label for _, label in FUNNEL_STEPS]
    fig, ax = _figure(width=3.5, height=2.1)
    y = list(range(len(values)))[::-1]
    ax.barh(y, values, height=0.6, color=SERIES_BLUE, edgecolor=SURFACE, linewidth=2)
    peak = max(values) or 1.0
    for yi, v in zip(y, values, strict=True):
        ax.text(v + peak * 0.01, yi, f"{v:,.0f}", va="center", fontsize=7.5, color=INK)
    ax.set_yticks(y, labels, color=INK_SECONDARY)
    ax.set_xlim(0, peak * 1.2)
    ax.set_xticks([])  # every bar carries its value; an axis would only add clutter
    ax.spines["bottom"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.set_title("Conversion funnel (sessions)")
    return _to_svg(fig)


KPI_CHANGES = (
    ("wow_sessions", "Sessions"),
    ("wow_purchases", "Purchases"),
    ("wow_revenue", "Revenue"),
    ("wow_conversion_rate", "Conversion rate"),
    ("wow_aov", "Average order value"),
)


def kpi_changes(weekly: pd.DataFrame, week: str) -> str:
    """Week-over-week change of headline KPIs (diverging bars around zero)."""
    row = weekly[(weekly["week"] == week) & (weekly["channel"] == TOTAL)].iloc[0]
    items = [(label, row[col]) for col, label in KPI_CHANGES]
    fig, ax = _figure(width=3.5, height=2.1)
    y = list(range(len(items)))[::-1]
    known = [float(v) for _, v in items if pd.notna(v)]
    if not known:
        ax.text(
            0.5,
            0.5,
            "No previous complete week to compare with",
            ha="center",
            va="center",
            color=INK_SECONDARY,
            transform=ax.transAxes,
        )
        ax.set_axis_off()
        ax.set_title("Change vs previous week")
        return _to_svg(fig)
    span = max(abs(v) for v in known) or 0.01
    for yi, (_label, v) in zip(y, items, strict=True):
        if pd.isna(v):
            ax.text(0, yi, "  n/a", va="center", fontsize=7.5, color=MUTED)
            continue
        value = float(v)
        ax.barh(yi, value, height=0.55, color=DIVERGING_UP if value >= 0 else DIVERGING_DOWN)
        offset = span * 0.03 if value >= 0 else -span * 0.03
        ax.text(
            value + offset,
            yi,
            f"{value:+.1%}",
            va="center",
            ha="left" if value >= 0 else "right",
            fontsize=7.5,
            color=INK,
        )
    ax.axvline(0, color=AXIS, linewidth=1)
    ax.set_yticks(y, [label for label, _ in items], color=INK_SECONDARY)
    ax.set_xlim(-span * 1.7, span * 1.7)  # room for value labels on both sides
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:+.0%}"))
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.set_title("Change vs previous week")
    return _to_svg(fig)
