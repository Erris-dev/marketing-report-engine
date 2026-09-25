"""Shared fixtures: a tiny synthetic dataset run through the real pipeline.

Three complete ISO weeks, 2020-W52 (Dec 21-27), 2020-W53 (Dec 28 - Jan 3) and
2021-W01 (Jan 4-10), so facts cover the year boundary. No BigQuery or network access.
"""

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from mre.config import AppConfig
from mre.facts import build_facts
from mre.pipeline import Prepared, prepare_from_frames
from mre.sources.sim_spend import simulate_spend

REPO_ROOT = Path(__file__).resolve().parents[1]
START, END = date(2020, 12, 21), date(2021, 1, 10)


def make_config(tmp_path: Path, **overrides: Any) -> AppConfig:
    data: dict[str, Any] = {
        "source": {
            "start_date": START.isoformat(),
            "end_date": END.isoformat(),
            "quarantine_dir": str(tmp_path / "quarantine"),
        },
        "channels": {"paid": ["paid_search"]},
        "spend_simulation": {"seasonality": False, "noise_sigma": 0.0},
        "llm": {
            "cache_dir": str(tmp_path / "llm_cache"),
            "system_prompt_path": str(REPO_ROOT / "prompts" / "narrative_system_v1.md"),
        },
    }
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return AppConfig.model_validate(data)


def synthetic_ga4() -> pd.DataFrame:
    rows = []
    day = START
    while day <= END:
        in_w01 = day >= date(2021, 1, 4)
        for channel, sessions, purchases, revenue in (
            ("paid_search", 50, 1, 40.0),
            ("direct", 200, 4, 300.0 if in_w01 else 200.0),
            ("unknown", 100, 1, 50.0),
        ):
            rows.append(
                {
                    "date": day,
                    "channel": channel,
                    "sessions": sessions,
                    "users": sessions - 5,
                    "view_item_sessions": sessions // 2,
                    "add_to_cart_sessions": sessions // 5,
                    "begin_checkout_sessions": sessions // 10,
                    "purchase_sessions": purchases,
                    "purchases": purchases,
                    "revenue": revenue,
                    "purchases_missing_revenue": 0,
                    "duplicate_purchase_events": 0,
                }
            )
        day += timedelta(days=1)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df


def prepare_synthetic(cfg: AppConfig, ga4: pd.DataFrame | None = None) -> Prepared:
    spend = simulate_spend(
        cfg.channels.paid, cfg.source.start_date, cfg.source.end_date, cfg.spend_simulation
    )
    return prepare_from_frames(synthetic_ga4() if ga4 is None else ga4, spend, cfg)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return make_config(tmp_path)


@pytest.fixture
def prepared(config: AppConfig) -> Prepared:
    return prepare_synthetic(config)


@pytest.fixture
def facts(prepared: Prepared, config: AppConfig) -> dict[str, Any]:
    return build_facts(
        "2021-W01", prepared.weekly, prepared.anomalies, prepared.quarantined_rows, config
    )
