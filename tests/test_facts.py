import json
from typing import Any

import pandas as pd
import pytest

from mre.anomalies import Anomaly
from mre.config import AppConfig
from mre.facts import build_facts, facts_hash
from mre.pipeline import Prepared

from .conftest import prepare_synthetic, synthetic_ga4


def _channel(facts: dict[str, Any], name: str) -> dict[str, Any]:
    return next(c for c in facts["channels"] if c["channel"] == name)


def test_facts_are_deterministic(prepared: Prepared, config: AppConfig) -> None:
    again = prepare_synthetic(config)
    a = build_facts("2021-W01", prepared.weekly, prepared.anomalies, 0, config)
    b = build_facts("2021-W01", again.weekly, again.anomalies, 0, config)
    assert a == b
    assert facts_hash(a) == facts_hash(b)


def test_facts_are_strict_json(facts: dict[str, Any]) -> None:
    json.dumps(facts, allow_nan=False)  # raises if any NaN/inf slipped through


def test_totals_and_rates_are_rounded_percentages(facts: dict[str, Any]) -> None:
    t = facts["totals"]
    # W01 per day: 350 sessions, 6 purchases; revenue 40 + 300 + 50 = 390
    assert t["sessions"] == 350 * 7
    assert t["purchases"] == 6 * 7
    assert t["revenue"] == 390.0 * 7
    assert t["conversion_rate_pct"] == 1.7  # 42 / 2450 = 1.714%
    assert t["aov"] == 65.0  # 2730 / 42
    # W53 revenue was 290/day -> (390 - 290) / 290 = 34.48%
    assert t["wow_revenue_pct"] == 34.5


def test_previous_week_crosses_year_boundary(prepared: Prepared, config: AppConfig) -> None:
    w01 = build_facts("2021-W01", prepared.weekly, prepared.anomalies, 0, config)
    w52 = build_facts("2020-W52", prepared.weekly, prepared.anomalies, 0, config)
    assert w01["previous_week"] == "2020-W53"
    assert w52["previous_week"] is None  # first week in the data has no comparison
    assert w52["totals"]["wow_revenue_pct"] is None


def test_spend_metrics_only_for_paid_and_flagged(facts: dict[str, Any]) -> None:
    assert facts["simulated_spend"] is True
    paid = _channel(facts, "paid_search")
    assert paid["simulated_spend"]["spend"] == 350.0  # 50/day, no noise or seasonality
    assert paid["simulated_spend"]["cost_per_session"] == 1.0  # 350 / 350
    assert paid["simulated_spend"]["roas"] == 0.8  # 280 / 350
    assert "simulated_spend" not in _channel(facts, "direct")


def test_data_quality_section(prepared: Prepared, config: AppConfig) -> None:
    facts = build_facts("2021-W01", prepared.weekly, prepared.anomalies, 3, config)
    dq = facts["data_quality"]
    assert dq["rows_quarantined"] == 3
    assert dq["days_covered"] == 7
    assert dq["unknown_channel_session_share_pct"] == 28.6  # 100 / 350


def test_quarantined_rows_are_counted_through_the_pipeline(config: AppConfig) -> None:
    ga4 = synthetic_ga4()
    ga4.loc[0, "revenue"] = -1.0
    prepared = prepare_synthetic(config, ga4)
    assert prepared.quarantined_rows == 1


def test_partial_and_unknown_weeks_are_rejected(prepared: Prepared, config: AppConfig) -> None:
    ga4 = synthetic_ga4()
    partial = prepare_synthetic(config, ga4[ga4["date"] != pd.Timestamp("2021-01-10")])
    with pytest.raises(ValueError, match="partial week"):
        build_facts("2021-W01", partial.weekly, [], 0, config)
    with pytest.raises(ValueError, match="No data"):
        build_facts("2021-W30", prepared.weekly, [], 0, config)


def _anomaly(**kw: Any) -> Anomaly:
    base: dict[str, Any] = {
        "channel": "direct",
        "metric": "revenue",
        "date_or_week": "2021-01-05",
        "granularity": "day",
        "rule": "robust_z",
        "value": 100.0,
        "baseline": 50.0,
        "score": 4.0,
        "direction": "up",
        "severity": "medium",
    }
    base.update(kw)
    return Anomaly(**base)


def test_anomalies_ranked_and_capped(prepared: Prepared, config: AppConfig) -> None:
    anomalies = [_anomaly(score=float(i)) for i in range(4, 20)] + [
        _anomaly(
            granularity="week",
            date_or_week="2021-W01",
            rule="tracking_gap",
            metric="missing_revenue_share",
            value=0.3,
            baseline=None,
            score=0.3,
            severity="high",
        ),
        _anomaly(date_or_week="2020-12-30", score=99.0),  # other week: excluded
    ]
    facts = build_facts("2021-W01", prepared.weekly, anomalies, 0, config)
    assert facts["anomaly_count"] == 17
    assert len(facts["anomalies"]) == config.report.max_anomalies
    first = facts["anomalies"][0]
    assert first["rule"] == "tracking_gap"
    assert first["value"] == 30.0  # stored as a percentage
    assert [a["z_score"] for a in facts["anomalies"][1:3]] == [19.0, 18.0]
