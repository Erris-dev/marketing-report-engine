"""KPIs, funnel rates and week-over-week changes.

Conventions:
- Any ratio with a zero or missing denominator is null (NaN), never infinity.
- Spend exists only for paid channels and is simulated; spend-based metrics are null
  for every other channel and for the all-channel ``total`` row.
- Weeks are ISO weeks. A week is ``is_complete`` only when all 7 days are present.
  Week-over-week compares a complete week with the complete week directly before it;
  otherwise the change is null. The dataset's first week (2020-W44, one day) is partial.
- ``missing_revenue_share`` is a data-quality ratio: in this dataset revenue tracking
  largely stops from 2021-01-26 while purchase sessions continue.
- ``users`` is not aggregated: distinct users are not additive across days or channels.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from mre.weeks import iso_week_label

TOTAL = "total"

SUM_COLUMNS = (
    "sessions",
    "view_item_sessions",
    "add_to_cart_sessions",
    "begin_checkout_sessions",
    "purchase_sessions",
    "purchases",
    "revenue",
    "spend",
    "purchases_missing_revenue",
    "duplicate_purchase_events",
)
RATIO_COLUMNS = (
    "conversion_rate",
    "aov",
    "cost_per_session",
    "cpa",
    "roas",
    "add_to_cart_rate",
    "checkout_rate",
    "purchase_rate",
    "missing_revenue_share",
)
SHARE_COLUMNS = ("session_share", "revenue_share")
SPEND_METRICS = ("spend", "cost_per_session", "cpa", "roas")
WOW_COLUMNS = ("sessions", "purchases", "revenue", "spend", *RATIO_COLUMNS)


def safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Element-wise division that yields NaN instead of inf when dividing by zero."""
    num = numerator.astype("float64")
    den = denominator.astype("float64")
    return num / den.where(den != 0)


def add_ratios(df: pd.DataFrame) -> pd.DataFrame:
    """Add KPI and funnel ratios to any frame that has the SUM_COLUMNS."""
    out = df.copy()
    out["conversion_rate"] = safe_divide(out["purchase_sessions"], out["sessions"])
    out["aov"] = safe_divide(out["revenue"], out["purchases"])
    out["cost_per_session"] = safe_divide(out["spend"], out["sessions"])
    out["cpa"] = safe_divide(out["spend"], out["purchases"])
    out["roas"] = safe_divide(out["revenue"], out["spend"])
    out["add_to_cart_rate"] = safe_divide(out["add_to_cart_sessions"], out["view_item_sessions"])
    out["checkout_rate"] = safe_divide(out["begin_checkout_sessions"], out["add_to_cart_sessions"])
    out["purchase_rate"] = safe_divide(out["purchase_sessions"], out["begin_checkout_sessions"])
    # Data quality: purchases whose revenue was not recorded (a tracking gap, not lost sales).
    out["missing_revenue_share"] = safe_divide(
        out["purchases_missing_revenue"], out["purchases"] + out["purchases_missing_revenue"]
    )
    return out


def join_spend(ga4: pd.DataFrame, spend: pd.DataFrame, paid: list[str]) -> pd.DataFrame:
    """Daily GA4 rows with simulated spend; spend stays null for non-paid channels."""
    daily = ga4.merge(spend[["date", "channel", "spend"]], on=["date", "channel"], how="left")
    is_paid = daily["channel"].isin(paid)
    # A paid channel with no spend row that day spent nothing; others have no spend concept.
    daily["spend"] = np.where(is_paid, daily["spend"].fillna(0.0), np.nan)
    daily["is_paid"] = is_paid
    return daily


def daily_metrics(ga4: pd.DataFrame, spend: pd.DataFrame, paid: list[str]) -> pd.DataFrame:
    return add_ratios(join_spend(ga4, spend, paid))


def weekly_metrics(ga4: pd.DataFrame, spend: pd.DataFrame, paid: list[str]) -> pd.DataFrame:
    """One row per ISO week and channel, plus a ``total`` row per week."""
    daily = join_spend(ga4, spend, paid)
    daily["week"] = [iso_week_label(d) for d in pd.to_datetime(daily["date"]).dt.date]

    # min_count=1 keeps spend null (not 0) for channels that never have spend.
    per_channel = daily.groupby(["week", "channel"], as_index=False).agg(
        **{c: (c, lambda s: s.sum(min_count=1)) for c in SUM_COLUMNS},
        is_paid=("is_paid", "first"),
    )
    totals = per_channel.groupby("week", as_index=False)[list(SUM_COLUMNS)].sum(min_count=1)
    totals["channel"] = TOTAL
    totals["is_paid"] = False
    weekly = pd.concat([per_channel, totals], ignore_index=True)

    days = daily.groupby("week")["date"].nunique().rename("days")
    weekly = weekly.join(days, on="week")
    weekly["is_complete"] = weekly["days"] == 7
    weekly["week_start"] = pd.to_datetime(
        [pd.Timestamp.fromisocalendar(int(w[:4]), int(w[-2:]), 1) for w in weekly["week"]]
    )

    weekly = add_ratios(weekly)
    # Spend-based metrics are only meaningful per paid channel.
    weekly.loc[~weekly["is_paid"], ["cost_per_session", "cpa", "roas"]] = np.nan
    weekly.loc[weekly["channel"] == TOTAL, "spend"] = np.nan

    channel_rows = weekly["channel"] != TOTAL
    for share, column in (("session_share", "sessions"), ("revenue_share", "revenue")):
        week_total = weekly[channel_rows].groupby("week")[column].transform("sum")
        weekly.loc[channel_rows, share] = safe_divide(weekly.loc[channel_rows, column], week_total)
        weekly.loc[~channel_rows, share] = 1.0

    return weekly.sort_values(["week_start", "channel"], ignore_index=True)


def add_week_over_week(weekly: pd.DataFrame) -> pd.DataFrame:
    """Add ``prev_*`` and ``wow_*`` (relative change) columns, and share changes in pp.

    The previous row is the same channel exactly 7 days earlier, and both weeks must be
    complete. That makes 2021-W01 compare against 2020-W53 across the year boundary.
    """
    previous = weekly[weekly["is_complete"]].copy()
    previous["week_start"] = previous["week_start"] + timedelta(days=7)
    keep = ["week_start", "channel", *WOW_COLUMNS, *SHARE_COLUMNS]
    previous = previous[keep].rename(columns={c: f"prev_{c}" for c in keep[2:]})

    out = weekly.merge(previous, on=["week_start", "channel"], how="left")
    incomplete = ~out["is_complete"]
    for column in (*WOW_COLUMNS, *SHARE_COLUMNS):
        out.loc[incomplete, f"prev_{column}"] = np.nan
    for column in WOW_COLUMNS:
        prev = out[f"prev_{column}"]
        out[f"wow_{column}"] = safe_divide(out[column] - prev, prev)
    for share in SHARE_COLUMNS:
        out[f"{share}_change_pp"] = (out[share] - out[f"prev_{share}"]) * 100
    return out


def complete_weeks(weekly: pd.DataFrame) -> list[str]:
    """Weeks eligible for a report, in order."""
    complete = weekly[weekly["is_complete"]].sort_values("week_start")
    return list(dict.fromkeys(complete["week"]))
