"""Configuration loading.

Behavior lives in ``config.yaml`` and secrets live in the environment (optionally a
``.env`` file). They are kept apart so the YAML can be committed and printed freely
while secrets never leave the process.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_ENV_PATH = Path(".env")

# ISO week label such as "2020-W49"; weeks 01-53 because 2020 has a week 53.
ISO_WEEK_PATTERN = r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$"


class _Strict(BaseModel):
    # Reject unknown keys so a typo in config.yaml fails loudly instead of being ignored.
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReportConfig(_Strict):
    timezone: str = "UTC"
    baseline_days: int = Field(28, gt=0)
    currency: str = Field("USD", min_length=3, max_length=3)


class ChannelsConfig(_Strict):
    paid: list[str] = Field(default_factory=list)


class PlantedAnomaly(_Strict):
    channel: str
    week: str = Field(pattern=ISO_WEEK_PATTERN)
    cost_multiplier: float = Field(gt=0)


class SpendSimulationConfig(_Strict):
    seed: int = 42
    cost_per_session: dict[str, float] = Field(default_factory=lambda: {"default": 0.60})
    daily_budget_base: dict[str, float] = Field(default_factory=lambda: {"default": 300.0})
    seasonality: bool = True
    planted_anomalies: list[PlantedAnomaly] = Field(default_factory=list)

    @field_validator("cost_per_session", "daily_budget_base")
    @classmethod
    def _needs_default_and_non_negative(cls, value: dict[str, float]) -> dict[str, float]:
        # A "default" entry means a newly added paid channel always has a value.
        if "default" not in value:
            raise ValueError("must contain a 'default' entry")
        if any(v < 0 for v in value.values()):
            raise ValueError("values must be non-negative")
        return value


class AnomalyRulesConfig(_Strict):
    cost_wow_increase: float = Field(0.40, gt=0)
    conversion_rate_wow_drop: float = Field(0.30, gt=0, lt=1)
    sessions_stable_max_change: float = Field(0.10, ge=0)
    revenue_share_change_pp: float = Field(15, gt=0, le=100)


class AnomalyConfig(_Strict):
    method: Literal["robust_z"] = "robust_z"
    z_threshold: float = Field(3.5, gt=0)
    min_baseline_days: int = Field(14, gt=0)
    rules: AnomalyRulesConfig = Field(default_factory=AnomalyRulesConfig)


class LLMConfig(_Strict):
    provider: Literal["openrouter"] = "openrouter"
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "google/gemini-3.1-flash-lite"
    temperature: float = Field(0.2, ge=0, le=2)
    max_retries: int = Field(1, ge=0)
    timeout_seconds: float = Field(60, gt=0)
    cache_dir: Path = Path("data/llm_cache")


class EmailConfig(_Strict):
    # Server settings are not secret; the login and recipients live in .env.
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(587, gt=0, lt=65536)
    use_starttls: bool = True
    subject_template: str = "Weekly marketing report {week}"


class DeliveryConfig(_Strict):
    method: Literal["email", "none"] = "none"
    email: EmailConfig = Field(default_factory=EmailConfig)


class AppConfig(_Strict):
    report: ReportConfig = Field(default_factory=ReportConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    spend_simulation: SpendSimulationConfig = Field(default_factory=SpendSimulationConfig)
    anomaly: AnomalyConfig = Field(default_factory=AnomalyConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)


class Secrets(BaseModel):
    """Secrets from the environment. SecretStr keeps them out of reprs and logs."""

    model_config = ConfigDict(frozen=True)

    openrouter_api_key: SecretStr | None = None
    gcp_project: str | None = None
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    report_recipients: list[str] = Field(default_factory=list)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Load and validate ``config.yaml``."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return AppConfig.model_validate(raw)


def read_env_file(path: Path) -> dict[str, str]:
    """Parse simple ``KEY=value`` lines.

    Deliberately tiny to avoid a dependency; it supports comments, blank lines and
    optional surrounding quotes, which is all ``.env.example`` uses.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_secrets(env_file: Path = DEFAULT_ENV_PATH) -> Secrets:
    """Read secrets, letting real environment variables override ``.env``."""
    merged = {**read_env_file(env_file), **os.environ}

    def get(name: str) -> str | None:
        # Empty strings (as in .env.example) mean "not set".
        return merged.get(name) or None

    recipients = [r.strip() for r in (get("REPORT_RECIPIENTS") or "").split(",") if r.strip()]
    return Secrets(
        openrouter_api_key=get("OPENROUTER_API_KEY"),
        gcp_project=get("GCP_PROJECT"),
        smtp_user=get("SMTP_USER"),
        smtp_password=get("SMTP_PASSWORD"),
        report_recipients=recipients,
    )
