"""Render smoke tests: tiny synthetic data -> whole pipeline -> HTML and PDF, LLM mocked.

The PDF test needs WeasyPrint and Pango, so it runs in Docker/CI and is skipped on a
machine without them (the HTML tests always run).
"""

from pathlib import Path
from typing import Any

import pytest

from mre import charts
from mre.config import AppConfig
from mre.narrative import AI_DISCLOSURE, TEMPLATE_DISCLOSURE
from mre.pipeline import Prepared
from mre.render import EMPTY, SIMULATED_NOTE, PdfUnavailableError, fmt_delta, render_report

from .test_narrative_guard import FakeClient, _good_reply


def _render(
    tmp_path: Path, config: AppConfig, prepared: Prepared, facts: dict[str, Any], **kw: Any
) -> Any:
    client = kw.pop("client", FakeClient([_good_reply(facts)]))
    return render_report(
        "2021-W01", config, None, tmp_path / "reports", client=client, prepared=prepared, **kw
    )


def test_html_report_has_sections_and_disclosures(
    tmp_path: Path, config: AppConfig, prepared: Prepared, facts: dict[str, Any]
) -> None:
    paths = _render(tmp_path, config, prepared, facts, pdf=False)
    html = paths.html.read_text(encoding="utf-8")
    assert paths.narrative.source == "llm"
    for expected in (
        "Week 2021-W01",
        "compared with 2020-W53",
        "AI-generated summary",
        "Key findings",
        "Things to check",
        "KPIs by channel",
        "SIMULATED SPEND",
        "Charts",
        "Anomalies",
        "Data quality",
        SIMULATED_NOTE,
        AI_DISCLOSURE,
    ):
        assert expected in html, expected
    assert html.count("data:image/svg+xml;base64,") == 4
    assert paths.pdf is None


def test_fallback_report_says_template_not_ai(
    tmp_path: Path, config: AppConfig, prepared: Prepared, facts: dict[str, Any]
) -> None:
    paths = _render(
        tmp_path, config, prepared, facts, client=FakeClient([RuntimeError("down")]), pdf=False
    )
    html = paths.html.read_text(encoding="utf-8")
    assert paths.narrative.source == "fallback"
    assert TEMPLATE_DISCLOSURE in html
    assert "Template summary" in html
    assert SIMULATED_NOTE in html


def test_pdf_smoke(
    tmp_path: Path, config: AppConfig, prepared: Prepared, facts: dict[str, Any]
) -> None:
    try:
        paths = _render(tmp_path, config, prepared, facts)
    except PdfUnavailableError:
        pytest.skip("WeasyPrint/Pango not available; the PDF test runs in Docker and CI")
    assert paths.pdf is not None
    data = paths.pdf.read_bytes()
    assert data.startswith(b"%PDF")
    assert len(data) > 10_000


@pytest.mark.parametrize(
    ("value", "higher_is_better", "text", "tone"),
    [
        (0.123, True, "▲ 12.3%", "good"),
        (-0.05, True, "▼ 5.0%", "bad"),
        (0.4, False, "▲ 40.0%", "bad"),  # cost up is bad
        (-0.4, False, "▼ 40.0%", "good"),  # cost down is good
        (0.1, None, "▲ 10.0%", "neutral"),  # spend: no judgement
        (0.0, True, "0.0%", "neutral"),
        (float("nan"), True, EMPTY, "none"),
        (None, True, EMPTY, "none"),
    ],
)
def test_fmt_delta(value: Any, higher_is_better: bool | None, text: str, tone: str) -> None:
    assert fmt_delta(value, higher_is_better) == {"text": text, "tone": tone}


def test_charts_render_svg(prepared: Prepared) -> None:
    weekly = prepared.weekly
    for svg in (
        charts.revenue_trend(weekly, "2021-W01", prepared.anomalies),
        charts.channel_mix(weekly, "2021-W01"),
        charts.funnel(weekly, "2021-W01"),
        charts.kpi_changes(weekly, "2021-W01"),
    ):
        assert svg.lstrip().startswith("<?xml") or "<svg" in svg[:500]


def test_kpi_chart_without_previous_week(prepared: Prepared) -> None:
    svg = charts.kpi_changes(prepared.weekly, "2020-W52")
    assert "No previous complete week" in svg


def test_channel_colors_are_fixed_per_channel() -> None:
    # Color follows the entity, never its rank.
    assert charts.CHANNEL_COLORS["paid_search"] == "#eda100"
    assert len(set(charts.CHANNEL_COLORS.values())) == len(charts.CHANNEL_COLORS)
    assert charts.channel_label("paid_search") == "Paid search"
