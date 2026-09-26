"""Assemble the weekly report: facts + narrative + charts -> HTML -> PDF.

HTML is always written (handy for review); the PDF needs WeasyPrint and its system
libraries (Pango), which is why PDFs are built inside Docker.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from mre import charts
from mre.anomalies import Anomaly
from mre.config import AppConfig
from mre.facts import build_facts
from mre.metrics import TOTAL
from mre.narrative import NarrativeResult, generate_narrative
from mre.pipeline import Prepared, prepare

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
SIMULATED_NOTE = "Ad spend and spend-based metrics are simulated."
EMPTY = "\u2013"  # en dash for missing values

_RULE_LABELS = {
    "robust_z": "Unusual day",
    "cost_increase": "Cost increase",
    "conversion_drop": "Conversion drop",
    "revenue_share_shift": "Revenue share shift",
    "paid_spend_no_purchases": "Spend, no purchases",
    "tracking_gap": "Tracking gap",
}
_METRIC_LABELS = {
    "sessions": "Sessions",
    "purchases": "Purchases",
    "revenue": "Revenue",
    "spend": "Spend",
    "conversion_rate": "Conversion rate",
    "cost_per_session": "Cost per session",
    "cpa": "CPA",
    "revenue_share": "Revenue share",
    "missing_revenue_share": "Purchases without revenue",
}
_MONEY = frozenset({"revenue", "spend", "cpa", "cost_per_session", "aov"})


class PdfUnavailableError(RuntimeError):
    """WeasyPrint or its system libraries are missing (build the PDF in Docker)."""


@dataclass(frozen=True)
class ReportPaths:
    html: Path
    pdf: Path | None
    narrative: NarrativeResult


# --- formatting ---------------------------------------------------------------------


def _missing(v: Any) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v))


def fmt_int(v: Any) -> str:
    return EMPTY if _missing(v) else f"{float(v):,.0f}"


def fmt_money(v: Any) -> str:
    return EMPTY if _missing(v) else f"${float(v):,.2f}"


def fmt_ratio_pct(v: Any) -> str:
    """0.123 -> 12.3%"""
    return EMPTY if _missing(v) else f"{float(v):.1%}"


def fmt_delta(v: Any, higher_is_better: bool | None = True) -> dict[str, str]:
    """Relative change as arrow + percent. The arrow gives direction; the tone says
    whether that is good (for costs, lower is better). Never color alone."""
    if _missing(v):
        return {"text": EMPTY, "tone": "none"}
    value = float(v)
    if value == 0:
        return {"text": "0.0%", "tone": "neutral"}
    arrow = "▲" if value > 0 else "▼"
    if higher_is_better is None:
        tone = "neutral"
    else:
        tone = "good" if (value > 0) == higher_is_better else "bad"
    return {"text": f"{arrow} {abs(value):.1%}", "tone": tone}


def _data_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


# --- context --------------------------------------------------------------------------


def _channel_rows(weekly: pd.DataFrame, week: str) -> list[dict[str, Any]]:
    rows = weekly[(weekly["week"] == week)].copy()
    rows["order"] = [
        len(charts.CHANNEL_ORDER) + 1
        if c == TOTAL
        else (
            charts.CHANNEL_ORDER.index(c)
            if c in charts.CHANNEL_ORDER
            else len(charts.CHANNEL_ORDER)
        )
        for c in rows["channel"]
    ]
    out = []
    for _, r in rows.sort_values("order").iterrows():
        out.append(
            {
                "channel": charts.channel_label(r["channel"]),
                "is_total": r["channel"] == TOTAL,
                "color": charts.CHANNEL_COLORS.get(r["channel"]),
                "sessions": fmt_int(r["sessions"]),
                "sessions_delta": fmt_delta(r["wow_sessions"]),
                "purchases": fmt_int(r["purchases"]),
                "purchases_delta": fmt_delta(r["wow_purchases"]),
                "revenue": fmt_money(r["revenue"]),
                "prev_revenue": fmt_money(r["prev_revenue"]),
                "revenue_delta": fmt_delta(r["wow_revenue"]),
                "conversion_rate": fmt_ratio_pct(r["conversion_rate"]),
                "conversion_delta": fmt_delta(r["wow_conversion_rate"]),
                "aov": fmt_money(r["aov"]),
                "revenue_share": fmt_ratio_pct(r["revenue_share"]),
            }
        )
    return out


def _paid_rows(weekly: pd.DataFrame, week: str, paid: list[str]) -> list[dict[str, Any]]:
    rows = weekly[(weekly["week"] == week) & weekly["channel"].isin(paid)]
    return [
        {
            "channel": charts.channel_label(r["channel"]),
            "spend": fmt_money(r["spend"]),
            "spend_delta": fmt_delta(r["wow_spend"], higher_is_better=None),
            "cost_per_session": fmt_money(r["cost_per_session"]),
            "cps_delta": fmt_delta(r["wow_cost_per_session"], higher_is_better=False),
            "cpa": fmt_money(r["cpa"]),
            "cpa_delta": fmt_delta(r["wow_cpa"], higher_is_better=False),
            "roas": EMPTY if _missing(r["roas"]) else f"{float(r['roas']):.2f}",
            "roas_delta": fmt_delta(r["wow_roas"]),
        }
        for _, r in rows.iterrows()
    ]


def _fmt_anomaly_value(metric: str, v: float | None, unit: str | None) -> str:
    if v is None:
        return EMPTY
    if unit == "%":
        return f"{v:.1f}%"
    return (
        fmt_money(v) if metric in _MONEY else fmt_int(v) if float(v).is_integer() else f"{v:,.2f}"
    )


def _anomaly_rows(facts: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "when": a["when"],
            "channel": charts.channel_label(a["channel"]),
            "metric": _METRIC_LABELS.get(a["metric"], a["metric"]),
            "rule": _RULE_LABELS.get(a["rule"], a["rule"]),
            "value": _fmt_anomaly_value(a["metric"], a["value"], a["unit"]),
            "baseline": _fmt_anomaly_value(a["metric"], a["baseline"], a["unit"]),
            "direction": "▲" if a["direction"] == "up" else "▼",
            "severity": a["severity"],
            "seasonal_context": a["seasonal_context"] or "",
            "simulated": a["simulated"],
        }
        for a in facts["anomalies"]
    ]


def _kpi_tiles(facts: dict[str, Any]) -> list[dict[str, Any]]:
    t = facts["totals"]
    tiles = [
        ("Sessions", fmt_int(t["sessions"]), t["wow_sessions_pct"]),
        ("Purchases", fmt_int(t["purchases"]), t["wow_purchases_pct"]),
        ("Revenue", fmt_money(t["revenue"]), t["wow_revenue_pct"]),
        ("Conversion rate", f"{t['conversion_rate_pct']:.1f}%", t["wow_conversion_rate_pct"]),
    ]
    return [
        {"label": label, "value": value, "delta": fmt_delta(None if d is None else d / 100)}
        for label, value, d in tiles
    ]


def build_context(
    facts: dict[str, Any],
    narrative: NarrativeResult,
    weekly: pd.DataFrame,
    anomalies: list[Anomaly],
    cfg: AppConfig,
) -> dict[str, Any]:
    week = facts["report_week"]
    return {
        "week": week,
        "week_start": facts["week_start"],
        "week_end": facts["week_end"],
        "previous_week": facts["previous_week"],
        "narrative": narrative.output,
        "narrative_source": narrative.source,
        "narrative_disclosure": narrative.disclosure,
        "simulated_note": SIMULATED_NOTE,
        "kpis": _kpi_tiles(facts),
        "channel_rows": _channel_rows(weekly, week),
        "paid_rows": _paid_rows(weekly, week, cfg.channels.paid),
        "charts": {
            "revenue_trend": _data_uri(charts.revenue_trend(weekly, week, anomalies)),
            "channel_mix": _data_uri(charts.channel_mix(weekly, week)),
            "funnel": _data_uri(charts.funnel(weekly, week)),
            "kpi_changes": _data_uri(charts.kpi_changes(weekly, week)),
        },
        "funnel_note": facts["funnel_pct"]["note"],
        "anomaly_rows": _anomaly_rows(facts),
        "anomaly_count": facts["anomaly_count"],
        "data_quality": facts["data_quality"],
        "currency": facts["currency"],
    }


# --- rendering ---------------------------------------------------------------------------


def render_html(context: dict[str, Any], templates_dir: Path = TEMPLATES_DIR) -> str:
    env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,  # a typo in the template fails loudly
    )
    return env.get_template("report.html.j2").render(**context)


def html_to_pdf(html: str, path: Path) -> None:
    try:
        from weasyprint import HTML  # heavy import with system deps; keep it lazy
    except (ImportError, OSError) as exc:
        raise PdfUnavailableError(
            "WeasyPrint is not available here. Build the PDF in Docker "
            "(see README), or install the 'pdf' extra with Pango on Linux."
        ) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(path)


def render_report(
    week: str,
    cfg: AppConfig,
    api_key: str | None,
    out_dir: Path,
    *,
    client: Any | None = None,
    prepared: Prepared | None = None,
    pdf: bool = True,
) -> ReportPaths:
    prepared = prepared or prepare(cfg)
    facts = build_facts(week, prepared.weekly, prepared.anomalies, prepared.quarantined_rows, cfg)
    narrative = generate_narrative(facts, cfg.llm, api_key, client=client)
    html = render_html(build_context(facts, narrative, prepared.weekly, prepared.anomalies, cfg))

    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{week}.html"
    html_path.write_text(html, encoding="utf-8")
    pdf_path: Path | None = None
    if pdf:
        pdf_path = out_dir / f"{week}.pdf"
        html_to_pdf(html, pdf_path)
        log.info("Wrote %s", pdf_path)
    return ReportPaths(html=html_path, pdf=pdf_path, narrative=narrative)
