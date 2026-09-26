"""CLI end-to-end on synthetic data. No BigQuery, no OpenRouter, no email.

Each test runs in a temporary directory with the API key blanked, so the real
``.env`` in the repo can never be picked up.
"""

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from mre.cli import app

from .conftest import END, REPO_ROOT, START, synthetic_ga4

runner = CliRunner()


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in ("OPENROUTER_API_KEY", "SMTP_USER", "SMTP_PASSWORD", "REPORT_RECIPIENTS"):
        monkeypatch.setenv(name, "")
    raw = tmp_path / "data" / "raw" / "ga4_daily.parquet"
    raw.parent.mkdir(parents=True)
    synthetic_ga4().to_parquet(raw, index=False)
    config = {
        "source": {
            "start_date": START.isoformat(),
            "end_date": END.isoformat(),
            "raw_path": str(raw),
            "quarantine_dir": str(tmp_path / "data" / "quarantine"),
        },
        "channels": {"paid": ["paid_search"]},
        "spend_simulation": {
            "seasonality": False,
            "output_path": str(tmp_path / "data" / "raw" / "sim_spend.parquet"),
        },
        "llm": {
            "cache_dir": str(tmp_path / "llm_cache"),
            "system_prompt_path": str(REPO_ROOT / "prompts" / "narrative_system_v2.md"),
        },
        "delivery": {"method": "none"},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return tmp_path


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
