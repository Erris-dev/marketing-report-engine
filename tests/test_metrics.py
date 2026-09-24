"""Hand-calculated metric tests.

Fixture: 2020-12-27 .. 2021-01-10, i.e. one day of 2020-W52 (partial), then the full
weeks 2020-W53 (Dec 28 - Jan 3) and 2021-W01 (Jan 4 - Jan 10), across the year boundary.

paid_search, every day: sessions 10, view_item 6, add_to_cart 3, begin_checkout 2,
purchase_sessions 1, purchases 1; revenue 20 and spend 5 in W52/W53, revenue 30 and
spend 7.5 in W01; in W01 each day also has 1 purchase with missing revenue.
direct, every day: sessions 30 and nothing else.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from mre.metrics import (
    TOTAL,
    add_week_over_week,
    complete_weeks,
    daily_metrics,
    safe_divide,
    weekly_metrics,
)
from mre.weeks import iso_week_label, previous_week, week_start

PAID = ["paid_search"]
START, END = date(2020, 12, 27), date(2021, 1, 10)
W01_START = date(2021, 1, 4)


def _days() -> list[date]:
    return [START + timedelta(days=i) for i in range((END - START).days + 1)]


def ga4_fixture() -> pd.DataFrame:
    rows = []
    for d in _days():
        in_w01 = d >= W01_START
        rows.append(
            {
                "date": d,
                "channel": "paid_search",
                "sessions": 10,
                "users": 9,
                "view_item_sessions": 6,
                "add_to_cart_sessions": 3,
                "begin_checkout_sessions": 2,
                "purchase_sessions": 1,
                "purchases": 1,
                "revenue": 30.0 if in_w01 else 20.0,
                "purchases_missing_revenue": 1 if in_w01 else 0,
                "duplicate_purchase_events": 0,
            }
        )
        rows.append(
            {
                "date": d,
                "channel": "direct",
                "sessions": 30,
                "users": 25,
                "view_item_sessions": 0,
                "add_to_cart_sessions": 0,
                "begin_checkout_sessions": 0,
                "purchase_sessions": 0,
                "purchases": 0,
                "revenue": 0.0,
                "purchases_missing_revenue": 0,
                "duplicate_purchase_events": 0,
            }
        )
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df


def spend_fixture() -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(_days()),
            "channel": "paid_search",
            "spend": [7.5 if d >= W01_START else 5.0 for d in _days()],
            "is_simulated": True,
        }
    )
    return df


@pytest.fixture
def weekly() -> pd.DataFrame:
    return add_week_over_week(weekly_metrics(ga4_fixture(), spend_fixture(), PAID))


def _row(weekly: pd.DataFrame, week: str, channel: str) -> pd.Series:
    match = weekly[(weekly["week"] == week) & (weekly["channel"] == channel)]
    assert len(match) == 1
    return match.iloc[0]


# --- safe_divide ---------------------------------------------------------------


def test_safe_divide_normal_zero_and_missing() -> None:
    result = safe_divide(pd.Series([1, 1, 0, 1]), pd.Series([4, 0, 0, np.nan]))
    assert result.iloc[0] == 0.25
    assert result.iloc[1:].isna().all()
    assert not np.isinf(result).any()


# --- weeks ---------------------------------------------------------------------


def test_iso_week_across_year_boundary() -> None:
    assert iso_week_label(date(2021, 1, 1)) == "2020-W53"
    assert iso_week_label(date(2021, 1, 4)) == "2021-W01"
    assert week_start("2020-W53") == date(2020, 12, 28)
    assert previous_week("2021-W01") == "2020-W53"


@pytest.mark.parametrize("label", ["2020-53", "2021-W53", "W01"])
def test_week_start_rejects_invalid_labels(label: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - message comes from the stdlib too
        week_start(label)


# --- weekly KPIs (paid_search, 2020-W53) ------------------------------------------


def test_paid_kpis_hand_calculated(weekly: pd.DataFrame) -> None:
    r = _row(weekly, "2020-W53", "paid_search")
    assert r["sessions"] == 70
    assert r["revenue"] == 140.0
    assert r["spend"] == 35.0
    assert r["conversion_rate"] == pytest.approx(7 / 70)  # 0.1
    assert r["aov"] == pytest.approx(140 / 7)  # 20
    assert r["cost_per_session"] == pytest.approx(35 / 70)  # 0.5
    assert r["cpa"] == pytest.approx(35 / 7)  # 5
    assert r["roas"] == pytest.approx(140 / 35)  # 4
    assert r["missing_revenue_share"] == 0.0


def test_funnel_rates_hand_calculated(weekly: pd.DataFrame) -> None:
    r = _row(weekly, "2020-W53", "paid_search")
    assert r["add_to_cart_rate"] == pytest.approx(21 / 42)  # 0.5
    assert r["checkout_rate"] == pytest.approx(14 / 21)  # 0.667
    assert r["purchase_rate"] == pytest.approx(7 / 14)  # 0.5


def test_missing_revenue_share(weekly: pd.DataFrame) -> None:
    # W01: 7 purchases with revenue, 7 without -> 7 / 14
    assert _row(weekly, "2021-W01", "paid_search")["missing_revenue_share"] == 0.5


# --- division by zero -------------------------------------------------------------


def test_zero_denominators_give_null_not_infinity(weekly: pd.DataFrame) -> None:
    r = _row(weekly, "2020-W53", "direct")
    assert r["conversion_rate"] == 0.0  # 0 / 210 is a real zero
    for metric in ("aov", "add_to_cart_rate", "checkout_rate", "purchase_rate"):
        assert np.isnan(r[metric]), metric
    numeric = weekly.select_dtypes("number")
    assert not np.isinf(numeric.to_numpy(dtype=float)).any()


def test_spend_metrics_null_for_non_paid_and_total(weekly: pd.DataFrame) -> None:
    for channel in ("direct", TOTAL):
        r = _row(weekly, "2020-W53", channel)
        for metric in ("cost_per_session", "cpa", "roas"):
            assert np.isnan(r[metric]), (channel, metric)
    assert np.isnan(_row(weekly, "2020-W53", "direct")["spend"])


def test_paid_channel_without_spend_row_counts_as_zero_spend() -> None:
    spend = spend_fixture()
    spend = spend[spend["date"] != pd.Timestamp("2020-12-28")]
    daily = daily_metrics(ga4_fixture(), spend, PAID)
    day = daily[(daily["date"] == "2020-12-28") & (daily["channel"] == "paid_search")].iloc[0]
    assert day["spend"] == 0.0
    assert np.isnan(day["roas"])  # revenue / 0 spend -> null


# --- totals and shares ------------------------------------------------------------


def test_total_row_and_shares(weekly: pd.DataFrame) -> None:
    total = _row(weekly, "2020-W53", TOTAL)
    assert total["sessions"] == 280  # 70 + 210
    assert total["revenue"] == 140.0
    assert total["conversion_rate"] == pytest.approx(7 / 280)
    assert _row(weekly, "2020-W53", "paid_search")["session_share"] == pytest.approx(0.25)
    assert _row(weekly, "2020-W53", "paid_search")["revenue_share"] == 1.0
    assert _row(weekly, "2020-W53", "direct")["revenue_share"] == 0.0


# --- partial weeks and week over week --------------------------------------------


def test_partial_week_is_flagged_and_excluded(weekly: pd.DataFrame) -> None:
    w52 = _row(weekly, "2020-W52", "paid_search")
    assert w52["days"] == 1
    assert not w52["is_complete"]
    assert complete_weeks(weekly) == ["2020-W53", "2021-W01"]


def test_no_week_over_week_against_a_partial_week(weekly: pd.DataFrame) -> None:
    r = _row(weekly, "2020-W53", "paid_search")
    assert np.isnan(r["prev_revenue"])
    assert np.isnan(r["wow_revenue"])


def test_week_over_week_across_year_boundary(weekly: pd.DataFrame) -> None:
    r = _row(weekly, "2021-W01", "paid_search")
    assert r["prev_revenue"] == 140.0  # from 2020-W53
    assert r["wow_revenue"] == pytest.approx((210 - 140) / 140)  # +50%
    assert r["wow_cpa"] == pytest.approx((7.5 - 5) / 5)  # +50%
    assert r["wow_roas"] == pytest.approx(0.0)  # 4 -> 4
    assert r["wow_sessions"] == 0.0
    assert r["revenue_share_change_pp"] == pytest.approx(0.0)


def test_week_over_week_null_when_previous_is_zero(weekly: pd.DataFrame) -> None:
    r = _row(weekly, "2021-W01", "direct")
    assert r["prev_revenue"] == 0.0
    assert np.isnan(r["wow_revenue"])
    assert r["wow_sessions"] == 0.0


def test_week_over_week_null_when_previous_week_missing() -> None:
    ga4 = ga4_fixture()
    ga4 = ga4[ga4["date"] >= pd.Timestamp(W01_START)]  # drop W52 and W53 entirely
    weekly = add_week_over_week(weekly_metrics(ga4, spend_fixture(), PAID))
    assert np.isnan(_row(weekly, "2021-W01", "paid_search")["wow_revenue"])
