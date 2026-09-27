"""Shared data preparation: cache -> validation -> metrics -> anomalies.

Spend is re-simulated on every run instead of read from disk: it is deterministic for a
given config, so this can never pick up a stale file.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from mre import schemas
from mre.anomalies import Anomaly, detect_anomalies
from mre.config import AppConfig
from mre.metrics import add_week_over_week, daily_metrics, weekly_metrics
from mre.sources import ga4_bigquery as ga4
from mre.sources.sim_spend import simulate_spend


@dataclass(frozen=True)
class Prepared:
    daily: pd.DataFrame
    weekly: pd.DataFrame  # with week-over-week columns
    anomalies: list[Anomaly]
    quarantined_rows: int


def prepare_from_frames(raw: pd.DataFrame, spend: pd.DataFrame, cfg: AppConfig) -> Prepared:
    src = cfg.source
    ga4_result = schemas.validate_and_quarantine(
        raw, schemas.ga4_daily_schema(src.start_date, src.end_date)
    )
    spend_result = schemas.validate_and_quarantine(
        spend, schemas.sim_spend_schema(src.start_date, src.end_date, cfg.channels.paid)
    )
    schemas.write_quarantine(ga4_result, "ga4_daily", src.quarantine_dir)
    schemas.write_quarantine(spend_result, "sim_spend", src.quarantine_dir)

    paid = cfg.channels.paid
    daily = daily_metrics(ga4_result.valid, spend_result.valid, paid)
    weekly = add_week_over_week(weekly_metrics(ga4_result.valid, spend_result.valid, paid))
    anomalies = detect_anomalies(daily, weekly, cfg.anomaly, paid, cfg.report.baseline_days)
    return Prepared(
        daily=daily,
        weekly=weekly,
        anomalies=anomalies,
        quarantined_rows=ga4_result.quarantined_count + spend_result.quarantined_count,
    )


def prepare(cfg: AppConfig) -> Prepared:
    raw = ga4.load_raw(cfg.source.input_path())
    spend = simulate_spend(
        cfg.channels.paid, cfg.source.start_date, cfg.source.end_date, cfg.spend_simulation
    )
    return prepare_from_frames(raw, spend, cfg)
