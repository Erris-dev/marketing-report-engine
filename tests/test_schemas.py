from collections.abc import Callable
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from mre.schemas import (
    GA4_COUNT_COLUMNS,
    StructuralValidationError,
    ga4_daily_schema,
    sim_spend_schema,
    validate_and_quarantine,
    write_quarantine,
)

START, END = date(2020, 11, 1), date(2021, 1, 31)
GA4 = ga4_daily_schema(START, END)
SIM = sim_spend_schema(START, END, ["paid_search"])


def ga4_rows() -> pd.DataFrame:
    """Two valid rows; each test breaks the second one."""
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-11-02", "2020-11-02"]),
            "channel": ["direct", "paid_search"],
            "sessions": [100, 50],
            "users": [80, 40],
            "view_item_sessions": [40, 20],
            "add_to_cart_sessions": [10, 5],
            "begin_checkout_sessions": [6, 3],
            "purchase_sessions": [3, 2],
            "purchases": [3, 2],
            "revenue": [150.0, 99.5],
            "purchases_missing_revenue": [0, 1],
            "duplicate_purchase_events": [0, 0],
        }
    )


def sim_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-11-02", "2020-11-03"]),
            "channel": ["paid_search", "paid_search"],
            "spend": [50.0, 48.2],
            "is_simulated": [True, True],
        }
    )


def _set(column: str, value: object) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def mutate(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df[column] = df[column].astype(object)
        df.at[1, column] = value
        return df.infer_objects()

    return mutate


def _duplicate_first(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.loc[1, ["date", "channel"]] = df.loc[0, ["date", "channel"]].to_numpy()
    return df


def test_valid_ga4_rows_pass() -> None:
    result = validate_and_quarantine(ga4_rows(), GA4)
    assert len(result.valid) == 2
    assert result.quarantined_count == 0


def test_valid_sim_rows_pass() -> None:
    result = validate_and_quarantine(sim_rows(), SIM)
    assert len(result.valid) == 2
    assert result.quarantined_count == 0


GA4_RULES = [
    *[(f"negative {c}", _set(c, -1), f"{c}: negative value") for c in GA4_COUNT_COLUMNS],
    ("negative revenue", _set("revenue", -5.0), "revenue: negative value"),
    ("purchases > sessions", _set("purchases", 51), "purchases > sessions"),
    ("purchase sessions > sessions", _set("purchase_sessions", 51), "purchase_sessions > sessions"),
    ("view_item > sessions", _set("view_item_sessions", 51), "view_item_sessions > sessions"),
    ("users > sessions", _set("users", 51), "users > sessions"),
    ("date before range", _set("date", pd.Timestamp("2020-10-31")), "date: date outside"),
    ("date after range", _set("date", pd.Timestamp("2021-02-01")), "date: date outside"),
    ("unknown channel label", _set("channel", "tiktok"), "channel: unknown channel label"),
    ("missing revenue", _set("revenue", None), "revenue: missing value"),
]


@pytest.mark.parametrize(("rule", "mutate", "reason"), GA4_RULES, ids=[r[0] for r in GA4_RULES])
def test_ga4_rule_quarantines_failing_row(
    rule: str, mutate: Callable[[pd.DataFrame], pd.DataFrame], reason: str
) -> None:
    result = validate_and_quarantine(mutate(ga4_rows()), GA4)
    assert list(result.valid["channel"]) == ["direct"]  # the good row survives
    assert result.quarantined_count == 1
    assert reason in result.quarantined["reason"].iloc[0]


def test_ga4_duplicates_are_all_quarantined() -> None:
    # Neither copy can be trusted over the other, so both go to quarantine.
    result = validate_and_quarantine(_duplicate_first(ga4_rows()), GA4)
    assert result.valid.empty
    assert list(result.quarantined["reason"]) == ["duplicate (date, channel)"] * 2


SIM_RULES = [
    ("negative spend", _set("spend", -0.01), "spend: negative value"),
    ("not simulated", _set("is_simulated", False), "is_simulated is not true"),
    ("non-paid channel", _set("channel", "organic_search"), "channel: not a paid channel"),
    ("date after range", _set("date", pd.Timestamp("2021-02-01")), "date: date outside"),
]


@pytest.mark.parametrize(("rule", "mutate", "reason"), SIM_RULES, ids=[r[0] for r in SIM_RULES])
def test_sim_rule_quarantines_failing_row(
    rule: str, mutate: Callable[[pd.DataFrame], pd.DataFrame], reason: str
) -> None:
    result = validate_and_quarantine(mutate(sim_rows()), SIM)
    assert len(result.valid) == 1
    assert result.quarantined_count == 1
    assert reason in result.quarantined["reason"].iloc[0]


def test_sim_duplicates_are_quarantined() -> None:
    result = validate_and_quarantine(_duplicate_first(sim_rows()), SIM)
    assert result.quarantined_count == 2
    assert set(result.quarantined["reason"]) == {"duplicate (date, channel)"}


def test_multiple_reasons_are_joined() -> None:
    df = _set("revenue", -1.0)(_set("purchases", 99)(ga4_rows()))
    reason = validate_and_quarantine(df, GA4).quarantined["reason"].iloc[0]
    assert reason == "purchases > sessions; revenue: negative value"


def test_missing_column_is_structural_not_quarantine() -> None:
    with pytest.raises(StructuralValidationError, match="revenue"):
        validate_and_quarantine(ga4_rows().drop(columns="revenue"), GA4)


def test_write_quarantine_always_writes_a_file(tmp_path: Path) -> None:
    clean = validate_and_quarantine(ga4_rows(), GA4)
    path = write_quarantine(clean, "ga4_daily", tmp_path)
    assert pd.read_csv(path).empty

    dirty = validate_and_quarantine(_set("revenue", -1.0)(ga4_rows()), GA4)
    written = pd.read_csv(write_quarantine(dirty, "ga4_daily", tmp_path))
    assert list(written["reason"]) == ["revenue: negative value"]
