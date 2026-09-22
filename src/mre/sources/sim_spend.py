"""Simulated daily ad spend for paid channels.

The GA4 sample has no cost data, so spend is generated:
``spend = daily_budget_base * seasonality_factor * noise * planted_multiplier``.
Every row carries ``is_simulated = True`` so spend-based metrics can always be labeled.

Each channel gets its own RNG seeded from (seed, channel name). That keeps output
identical for a given seed and means adding a channel never changes another's numbers.
"""

from __future__ import annotations

import zlib
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from mre.config import SpendSimulationConfig

COLUMNS = ("date", "channel", "spend", "is_simulated", "planted_multiplier")


def thanksgiving(year: int) -> date:
    """US Thanksgiving: the fourth Thursday of November."""
    nov1 = date(year, 11, 1)
    first_thursday = nov1 + timedelta(days=(3 - nov1.weekday()) % 7)
    return first_thursday + timedelta(weeks=3)


def seasonality_factor(day: date) -> float:
    """Budget multiplier for US holiday retail peaks.

    Advertisers typically raise budgets from Thanksgiving through Cyber Monday, keep
    them elevated until shipping cut-off before Christmas, and cut them on the holidays.
    """
    tg = thanksgiving(day.year)
    if tg <= day <= tg + timedelta(days=4):  # Thanksgiving .. Cyber Monday
        return 1.8
    if date(day.year, 12, 1) <= day <= date(day.year, 12, 18):
        return 1.3
    if date(day.year, 12, 19) <= day <= date(day.year, 12, 24):
        return 1.1
    if date(day.year, 12, 25) <= day <= date(day.year, 12, 26):
        return 0.6
    return 1.0


def iso_week_label(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _channel_rng(seed: int, channel: str) -> np.random.Generator:
    # crc32 rather than hash(): Python's str hash is salted per process.
    return np.random.default_rng([seed, zlib.crc32(channel.encode("utf-8"))])


def _validate_planted(
    cfg: SpendSimulationConfig, channels: list[str], weeks_in_range: set[str]
) -> None:
    for planted in cfg.planted_anomalies:
        if planted.channel not in channels:
            raise ValueError(
                f"Planted anomaly channel {planted.channel!r} is not a paid channel {channels}"
            )
        if planted.week not in weeks_in_range:
            raise ValueError(f"Planted anomaly week {planted.week} is outside the date range")


def simulate_spend(
    channels: list[str], start: date, end: date, cfg: SpendSimulationConfig
) -> pd.DataFrame:
    """One row per date and paid channel, deterministic for a given config."""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    weeks = [iso_week_label(d) for d in days]
    _validate_planted(cfg, channels, set(weeks))

    season = np.array([seasonality_factor(d) if cfg.seasonality else 1.0 for d in days])
    frames = []
    for channel in channels:
        rng = _channel_rng(cfg.seed, channel)
        # Mean-one log-normal noise: multiplicative and never negative.
        sigma = cfg.noise_sigma
        noise = rng.lognormal(mean=-(sigma**2) / 2, sigma=sigma, size=len(days))
        planted = np.ones(len(days))
        for anomaly in cfg.planted_anomalies:
            if anomaly.channel == channel:
                in_week = np.array([w == anomaly.week for w in weeks])
                planted[in_week] *= anomaly.cost_multiplier
        spend = cfg.budget_for(channel) * season * noise * planted
        frames.append(
            pd.DataFrame(
                {
                    "date": pd.to_datetime(days),
                    "channel": channel,
                    "spend": np.round(spend, 2),
                    "is_simulated": True,
                    "planted_multiplier": planted,
                }
            )
        )
    if not frames:
        return pd.DataFrame({c: pd.Series(dtype=object) for c in COLUMNS})
    return pd.concat(frames, ignore_index=True).sort_values(["date", "channel"], ignore_index=True)


def write_sim_spend(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
