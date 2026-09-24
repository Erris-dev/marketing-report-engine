from datetime import date
from typing import Any

import pandas as pd
import pytest

from mre.config import SpendSimulationConfig
from mre.sources.sim_spend import (
    seasonality_factor,
    simulate_spend,
    thanksgiving,
)
from mre.weeks import iso_week_label

START, END = date(2020, 11, 1), date(2021, 1, 31)


def _cfg(**overrides: Any) -> SpendSimulationConfig:
    return SpendSimulationConfig.model_validate(overrides)


def _run(channels: list[str] | None = None, **overrides: Any) -> pd.DataFrame:
    return simulate_spend(channels or ["paid_search"], START, END, _cfg(**overrides))


def test_same_seed_gives_identical_output() -> None:
    pd.testing.assert_frame_equal(_run(seed=7), _run(seed=7))


def test_different_seed_changes_output() -> None:
    assert not _run(seed=7)["spend"].equals(_run(seed=8)["spend"])


def test_adding_a_channel_does_not_change_existing_channel() -> None:
    alone = _run(["paid_search"])
    both = _run(["paid_search", "paid_social"])
    pd.testing.assert_frame_equal(
        both[both["channel"] == "paid_search"].reset_index(drop=True), alone
    )


def test_shape_and_labels() -> None:
    df = _run(["paid_search", "paid_social"])
    assert len(df) == 92 * 2
    assert not df.duplicated(["date", "channel"]).any()
    assert df["date"].min() == pd.Timestamp(START)
    assert df["date"].max() == pd.Timestamp(END)
    assert df["is_simulated"].all()


@pytest.mark.parametrize("sigma", [0.0, 0.1, 1.0])
def test_spend_is_never_negative(sigma: float) -> None:
    df = _run(["a", "b", "c"], noise_sigma=sigma, daily_budget_base={"default": 50, "b": 0})
    assert (df["spend"] >= 0).all()


def test_no_noise_no_seasonality_equals_budget() -> None:
    # Hand-calculated: 50 * 1.0 * 1.0 = 50 every day.
    df = _run(noise_sigma=0, seasonality=False)
    assert (df["spend"] == 50.0).all()


def test_budget_times_seasonality_without_noise() -> None:
    df = _run(noise_sigma=0).set_index("date")["spend"]
    assert df[pd.Timestamp("2020-11-27")] == 90.0  # Black Friday: 50 * 1.8
    assert df[pd.Timestamp("2020-12-10")] == 65.0  # December: 50 * 1.3
    assert df[pd.Timestamp("2020-12-25")] == 30.0  # Christmas: 50 * 0.6
    assert df[pd.Timestamp("2021-01-15")] == 50.0  # normal day


def test_planted_anomaly_lands_in_the_right_week_only() -> None:
    planted = [{"channel": "paid_search", "week": "2020-W49", "cost_multiplier": 2.0}]
    base = _run()
    with_anomaly = _run(planted_anomalies=planted)
    ratio = with_anomaly["spend"] / base["spend"]
    in_week = base["date"].dt.date.map(iso_week_label) == "2020-W49"

    assert in_week.sum() == 7
    assert ratio[in_week].round(2).eq(2.0).all()
    assert ratio[~in_week].eq(1.0).all()
    # 2020-W49 is Monday 2020-11-30 .. Sunday 2020-12-06
    week_dates = base.loc[in_week, "date"]
    assert week_dates.min() == pd.Timestamp("2020-11-30")
    assert week_dates.max() == pd.Timestamp("2020-12-06")


def test_planted_anomaly_across_year_boundary() -> None:
    # 2020-W53 runs Monday 2020-12-28 .. Sunday 2021-01-03.
    planted = [{"channel": "paid_search", "week": "2020-W53", "cost_multiplier": 3.0}]
    df = _run(planted_anomalies=planted)
    hit = df.loc[df["planted_multiplier"] == 3.0, "date"]
    assert list(hit.dt.date) == [date(2020, 12, 28 + i) for i in range(4)] + [
        date(2021, 1, d) for d in (1, 2, 3)
    ]


def test_planted_anomaly_on_unknown_channel_is_rejected() -> None:
    planted = [{"channel": "organic_search", "week": "2020-W49", "cost_multiplier": 2.0}]
    with pytest.raises(ValueError, match="not a paid channel"):
        _run(planted_anomalies=planted)


def test_planted_anomaly_outside_range_is_rejected() -> None:
    planted = [{"channel": "paid_search", "week": "2021-W30", "cost_multiplier": 2.0}]
    with pytest.raises(ValueError, match="outside the date range"):
        _run(planted_anomalies=planted)


@pytest.mark.parametrize(
    ("year", "expected"), [(2020, date(2020, 11, 26)), (2021, date(2021, 11, 25))]
)
def test_thanksgiving(year: int, expected: date) -> None:
    assert thanksgiving(year) == expected


@pytest.mark.parametrize(
    ("day", "factor"),
    [
        (date(2020, 11, 25), 1.0),  # day before Thanksgiving
        (date(2020, 11, 26), 1.8),  # Thanksgiving
        (date(2020, 11, 30), 1.8),  # Cyber Monday
        (date(2020, 12, 1), 1.3),
        (date(2020, 12, 18), 1.3),
        (date(2020, 12, 19), 1.1),
        (date(2020, 12, 26), 0.6),
        (date(2020, 12, 27), 1.0),
    ],
)
def test_seasonality_factor(day: date, factor: float) -> None:
    assert seasonality_factor(day) == factor


def test_no_paid_channels_gives_empty_frame() -> None:
    df = simulate_spend([], START, END, _cfg())
    assert df.empty
    assert "is_simulated" in df.columns
