from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from mre.cli import app
from mre.config import AppConfig, load_config, load_secrets, read_env_file

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_repo_config_loads() -> None:
    cfg = load_config(REPO_ROOT / "config.yaml")
    assert cfg.llm.model == "google/gemini-3.1-flash-lite"
    assert cfg.spend_simulation.seed == 42
    assert cfg.anomaly.rules.cost_wow_increase == pytest.approx(0.40)


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"report": {"baseline_dayz": 28}})


@pytest.mark.parametrize("week", ["2020-W53", "2021-W01"])
def test_planted_anomaly_accepts_iso_week(week: str) -> None:
    cfg = AppConfig.model_validate(
        {
            "spend_simulation": {
                "planted_anomalies": [{"channel": "x", "week": week, "cost_multiplier": 2}]
            }
        }
    )
    assert cfg.spend_simulation.planted_anomalies[0].week == week


@pytest.mark.parametrize("week", ["2020-W54", "2020-49", "2020-W00"])
def test_planted_anomaly_rejects_bad_week(week: str) -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "spend_simulation": {
                    "planted_anomalies": [{"channel": "x", "week": week, "cost_multiplier": 2}]
                }
            }
        )


def test_spend_maps_require_default() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"spend_simulation": {"cost_per_session": {"cpc": 0.5}}})


def test_env_file_parsing_and_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text('# comment\nOPENROUTER_API_KEY="sk-file"\nGCP_PROJECT=\nTELEGRAM_CHAT_ID=1\n')
    assert read_env_file(env)["OPENROUTER_API_KEY"] == "sk-file"

    for name in ("OPENROUTER_API_KEY", "GCP_PROJECT", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99")

    secrets = load_secrets(env)
    assert secrets.openrouter_api_key is not None
    assert secrets.openrouter_api_key.get_secret_value() == "sk-file"
    assert "sk-file" not in repr(secrets)
    assert secrets.gcp_project is None
    assert secrets.telegram_chat_id == "99"


def test_cli_help() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "check-config" in result.output
