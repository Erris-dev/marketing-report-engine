from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from mre.anomalies import (
    Anomaly,
    anomalies_for_week,
    detect_anomalies,
    robust_z,
    rule_anomalies,
    seasonal_note,
    statistical_anomalies,
)
from mre.config import AnomalyConfig, SpendSimulationConfig
from mre.metrics import TOTAL, add_week_over_week, daily_metrics, weekly_metrics
from mre.sources.sim_spend import simulate_spend
from mre.weeks import iso_week_label

START, END = date(2020, 11, 1), date(2021, 1, 31)
PAID = ["paid_search"]
CFG = AnomalyConfig()
BASELINE_DAYS = 28


# --- robust z-score ----------------------------------------------------------------


def test_robust_z_hand_calculated() -> None:
    # median 3, |deviations| [2,1,0,1,2] -> MAD 1; z = 0.6745 * (10 - 3) / 1
    assert robust_z(10, np.array([1.0, 2, 3, 4, 5])) == pytest.approx(4.7215)


def test_robust_z_falls_back_to_mean_absolute_deviation_when_mad_is_zero() -> None:
    # median 5, deviations [0,0,0,0,4] -> MAD 0, MeanAD 0.8; z = 4 / (1.253314 * 0.8)
    assert robust_z(9, np.array([5.0, 5, 5, 5, 9])) == pytest.approx(3.98942, abs=1e-4)


def test_robust_z_skips_constant_baseline() -> None:
    assert robust_z(9, np.array([5.0] * 20)) is None


# --- synthetic data ------------------------------------------------------------------


def _days() -> list[date]:
    return [START + timedelta(days=i) for i in range((END - START).days + 1)]


def quiet_ga4(spikes: dict[tuple[str, date], dict[str, float]] | None = None) -> pd.DataFrame:
    """Flat series with small bounded noise, so no honest anomaly exists."""
    rng = np.random.default_rng(0)
    rows = []
    for d in _days():
        for channel in ("paid_search", "direct"):
            jitter = int(rng.integers(-2, 3))  # bounded: at most +-2
            row: dict[str, Any] = {
                "date": d,
                "channel": channel,
                "sessions": 100 + jitter,
                "users": 90,
                "view_item_sessions": 50,
                "add_to_cart_sessions": 20,
                "begin_checkout_sessions": 12,
                "purchase_sessions": 10,
                "purchases": 10,
                "revenue": 500.0 + 5 * jitter,
                "purchases_missing_revenue": 0,
                "duplicate_purchase_events": 0,
            }
            row.update((spikes or {}).get((channel, d), {}))
            rows.append(row)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _spend(**overrides: Any) -> pd.DataFrame:
    cfg = SpendSimulationConfig.model_validate({"seasonality": False, **overrides})
    return simulate_spend(PAID, START, END, cfg)


def _detect(ga4: pd.DataFrame, spend: pd.DataFrame, cfg: AnomalyConfig = CFG) -> list[Anomaly]:
    daily = daily_metrics(ga4, spend, PAID)
    weekly = add_week_over_week(weekly_metrics(ga4, spend, PAID))
    return detect_anomalies(daily, weekly, cfg, PAID, BASELINE_DAYS)


def test_quiet_series_produces_no_anomalies() -> None:
    assert _detect(quiet_ga4(), _spend(noise_sigma=0.02)) == []


def test_planted_anomaly_is_detected_in_its_week() -> None:
    planted = [{"channel": "paid_search", "week": "2021-W02", "cost_multiplier": 2.0}]
    found = _detect(quiet_ga4(), _spend(noise_sigma=0.02, planted_anomalies=planted))

    spend_days = {a.date_or_week for a in found if a.metric == "spend" and a.rule == "robust_z"}
    week_days = {(date(2021, 1, 11) + timedelta(days=i)).isoformat() for i in range(7)}
    assert spend_days == week_days
    assert all(a.direction == "up" and a.is_simulated for a in found if a.metric == "spend")

    rules = {(a.rule, a.metric) for a in found if a.week == "2021-W02" and a.granularity == "week"}
    assert ("cost_increase", "cpa") in rules
    assert ("cost_increase", "cost_per_session") in rules


def test_black_friday_spike_is_flagged_with_seasonal_context() -> None:
    black_friday = date(2020, 11, 27)
    spikes = {("direct", black_friday): {"revenue": 2000.0, "purchases": 40}}
    found = _detect(quiet_ga4(spikes), _spend(noise_sigma=0.02))
    hits = [a for a in found if a.date_or_week == black_friday.isoformat()]
    assert {(a.channel, a.metric) for a in hits} >= {("direct", "revenue"), (TOTAL, "revenue")}
    for a in hits:
        assert a.seasonal_context is not None
        assert "Black Friday" in a.seasonal_context


def test_short_baseline_is_skipped() -> None:
    # A spike on day 10 has only 9 baseline days (< min_baseline_days = 14).
    spike_day = START + timedelta(days=9)
    spikes = {("direct", spike_day): {"sessions": 1000}}
    daily = daily_metrics(quiet_ga4(spikes), _spend(), PAID)
    found = statistical_anomalies(daily, CFG, BASELINE_DAYS)
    assert [a for a in found if a.date_or_week == spike_day.isoformat()] == []


def test_low_volume_series_are_not_scored() -> None:
    ga4 = quiet_ga4()
    # 0, 1, 2, 0, 1, 2, ... purchases a day (median 1, MAD 1), like real paid_search
    ga4["purchases"] = (ga4["date"].dt.dayofyear % 3).astype(int)
    spike_day = pd.Timestamp("2020-12-15")
    # z = 0.6745 * (8 - 1) / 1 = 4.72: flagged only when the volume guard is off
    ga4.loc[(ga4["date"] == spike_day) & (ga4["channel"] == "direct"), "purchases"] = 8
    daily = daily_metrics(ga4, _spend(), PAID)

    def purchase_flags(cfg: AnomalyConfig) -> list[Anomaly]:
        found = statistical_anomalies(daily, cfg, BASELINE_DAYS)
        return [a for a in found if a.metric == "purchases" and a.channel == "direct"]

    assert purchase_flags(CFG) == []
    assert len(purchase_flags(AnomalyConfig(min_daily_volume=0))) == 1


# --- weekly rules ----------------------------------------------------------------------


def _week(**overrides: Any) -> pd.DataFrame:
    """One complete weekly row with nothing unusual in it."""
    row: dict[str, Any] = {
        "week": "2021-W03",
        "channel": "paid_search",
        "is_complete": True,
        "spend": 350.0,
        "purchases": 10,
        "prev_purchases": 10,
        "cpa": 35.0,
        "prev_cpa": 35.0,
        "wow_cpa": 0.0,
        "cost_per_session": 0.5,
        "prev_cost_per_session": 0.5,
        "wow_cost_per_session": 0.0,
        "conversion_rate": 0.02,
        "prev_conversion_rate": 0.02,
        "wow_conversion_rate": 0.0,
        "wow_sessions": 0.0,
        "revenue_share": 0.1,
        "prev_revenue_share": 0.1,
        "revenue_share_change_pp": 0.0,
        "missing_revenue_share": 0.0,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def _rules(**overrides: Any) -> list[tuple[str, str, str]]:
    return [(a.rule, a.metric, a.severity) for a in rule_anomalies(_week(**overrides), CFG, PAID)]


def test_normal_week_triggers_no_rule() -> None:
    assert _rules() == []


def test_paid_spend_without_purchases() -> None:
    assert _rules(purchases=0) == [("paid_spend_no_purchases", "purchases", "high")]
    assert _rules(purchases=0, spend=0.0) == []
    assert _rules(purchases=0, channel="direct", spend=np.nan) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (0.39, []),
        (0.5, [("cost_increase", "cpa", "medium")]),
        (1.5, [("cost_increase", "cpa", "high")]),
    ],
)
def test_cpa_increase(change: float, expected: list[tuple[str, str, str]]) -> None:
    assert _rules(wow_cpa=change) == expected


def test_cost_per_session_increase() -> None:
    assert _rules(wow_cost_per_session=0.45) == [("cost_increase", "cost_per_session", "medium")]


def test_conversion_drop_needs_stable_sessions() -> None:
    assert _rules(wow_conversion_rate=-0.4, wow_sessions=0.05) == [
        ("conversion_drop", "conversion_rate", "medium")
    ]
    assert _rules(wow_conversion_rate=-0.4, wow_sessions=0.25) == []
    assert _rules(wow_conversion_rate=-0.2, wow_sessions=0.0) == []


def test_revenue_share_shift() -> None:
    assert _rules(revenue_share_change_pp=16.0) == [
        ("revenue_share_shift", "revenue_share", "medium")
    ]
    assert _rules(revenue_share_change_pp=-31.0) == [
        ("revenue_share_shift", "revenue_share", "high")
    ]
    assert _rules(revenue_share_change_pp=10.0) == []
    assert _rules(revenue_share_change_pp=40.0, channel=TOTAL, spend=np.nan) == []


def test_tracking_gap_only_on_total() -> None:
    assert _rules(channel=TOTAL, spend=np.nan, missing_revenue_share=0.3) == [
        ("tracking_gap", "missing_revenue_share", "high")
    ]
    assert _rules(channel=TOTAL, spend=np.nan, missing_revenue_share=0.1) == []
    assert _rules(missing_revenue_share=0.9) == []  # per-channel rows are not repeated


def test_incomplete_weeks_are_ignored() -> None:
    assert _rules(is_complete=False, purchases=0, wow_cpa=3.0) == []


# --- helpers --------------------------------------------------------------------------


def test_daily_anomaly_belongs_to_its_iso_week() -> None:
    a = Anomaly(
        channel="direct",
        metric="revenue",
        date_or_week="2021-01-01",
        granularity="day",
        rule="robust_z",
        value=1.0,
        baseline=0.5,
        score=4.0,
        direction="up",
        severity="medium",
    )
    assert a.week == "2020-W53" == iso_week_label(date(2021, 1, 1))
    assert anomalies_for_week([a], "2020-W53") == [a]
    assert anomalies_for_week([a], "2021-W01") == []


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2020, 11, 27), "Thanksgiving to Cyber Monday (Black Friday period)"),
        (date(2020, 12, 10), "holiday shopping season"),
        (date(2020, 12, 25), "Christmas"),
        (date(2021, 1, 1), "New Year"),
        (date(2021, 1, 15), None),
    ],
)
def test_seasonal_note(day: date, expected: str | None) -> None:
    assert seasonal_note(day) == expected
