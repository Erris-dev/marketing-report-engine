"""CLI end-to-end on synthetic data. No BigQuery, no OpenRouter, no email.

Each test runs in a temporary directory with the API key blanked, so the real
``.env`` in the repo can never be picked up.
"""

from pathlib import Path

from typer.testing import CliRunner

from mre.cli import app

runner = CliRunner()


def test_run_builds_html_and_skips_delivery(workdir: Path) -> None:
    result = runner.invoke(app, ["run", "--week", "2021-W01", "--html-only", "--no-deliver"])
    assert result.exit_code == 0, result.output
    assert "narrative: fallback" in result.output  # no API key -> template
    assert (workdir / "out" / "reports" / "2021-W01.html").exists()


def test_run_rejects_partial_or_unknown_week(workdir: Path) -> None:
    result = runner.invoke(app, ["run", "--week", "2021-W30", "--html-only"])
    assert result.exit_code == 1


def test_backfill_writes_every_complete_week(workdir: Path) -> None:
    result = runner.invoke(app, ["backfill", "--html-only"])
    assert result.exit_code == 0, result.output
    written = sorted(p.stem for p in (workdir / "out" / "reports").glob("*.html"))
    assert written == ["2020-W52", "2020-W53", "2021-W01"]
    assert "3 reports written" in result.output


def test_validate_simulate_and_facts(workdir: Path) -> None:
    assert runner.invoke(app, ["simulate-spend"]).exit_code == 0
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 0, result.output
    assert "ga4_daily: 63 valid, 0 quarantined" in result.output
    facts = runner.invoke(app, ["facts", "--week", "2021-W01"])
    assert facts.exit_code == 0
    assert (workdir / "out" / "facts" / "2021-W01.json").exists()


def test_extract_uses_cache_without_bigquery(workdir: Path) -> None:
    result = runner.invoke(app, ["extract"])
    assert result.exit_code == 0, result.output
    assert "not querying BigQuery" in result.output
