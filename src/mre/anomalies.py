"""Anomaly detection: a robust z-score on daily series plus explicit weekly rules.

Statistical check, per channel (and the all-channel total) and metric:
``z = 0.6745 * (x - median) / MAD`` against the trailing ``baseline_days`` days,
excluding the day itself. Days with fewer than ``min_baseline_days`` non-null baseline
values are skipped.

MAD fallback: when MAD is 0 (more than half the baseline is one value), the mean
absolute deviation is used instead, ``z = (x - median) / (1.253314 * MeanAD)``, the
standard fallback for the modified z-score (1.253314 = sqrt(pi/2) makes MeanAD
comparable to a standard deviation). If MeanAD is also 0 the baseline is constant and
the day is skipped, because any change would give an infinite score.

Seasonal peaks are flagged like anything else, but carry a ``seasonal_context`` note so
the report explains them instead of hiding them.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from mre.config import AnomalyConfig
from mre.metrics import TOTAL, add_ratios
from mre.sources.sim_spend import thanksgiving
from mre.weeks import iso_week_label, week_start

SPEND_BASED = frozenset({"spend", "cost_per_session", "cpa", "roas"})
# The count a metric rests on. A day is only scored when the baseline median of that
# count reaches ``min_daily_volume``: with ~1 purchase a day the median is 0 and a single
# purchase looks extreme. Weekly rules still cover low-volume channels.
VOLUME_BASIS = {
    "sessions": "sessions",
    "purchases": "purchases",
    "revenue": "purchases",
    "conversion_rate": "purchase_sessions",
    "cost_per_session": "sessions",
}
_MEAN_AD_SCALE = 1.253314
_Z_SCALE = 0.6745


class Anomaly(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel: str
    metric: str
    date_or_week: str  # "2021-01-12" for daily, "2021-W02" for weekly
    granularity: Literal["day", "week"]
    rule: str
    value: float | None
    baseline: float | None
    score: float | None  # z-score for robust_z, relative change or pp for rules
    direction: Literal["up", "down"]
    severity: Literal["medium", "high"]
    seasonal_context: str | None = None
    is_simulated: bool = False

    @property
    def week(self) -> str:
        if self.granularity == "week":
            return self.date_or_week
        return iso_week_label(date.fromisoformat(self.date_or_week))


def _num(value: object) -> float | None:
    """NaN-safe conversion for model fields."""
    if value is None:
        return None
    f = float(value)  # type: ignore[arg-type]
    return None if np.isnan(f) else f


# --- seasonal context ------------------------------------------------------------


def seasonal_note(day: date) -> str | None:
    tg = thanksgiving(day.year)
    if tg <= day <= tg + timedelta(days=4):
        return "Thanksgiving to Cyber Monday (Black Friday period)"
    if date(day.year, 12, 25) <= day <= date(day.year, 12, 26):
        return "Christmas"
    if date(day.year, 12, 1) <= day <= date(day.year, 12, 24):
        return "holiday shopping season"
    if (day.month, day.day) in {(12, 31), (1, 1)}:
        return "New Year"
    return None


def _context_for_day(day: date, baseline_days: int) -> str | None:
    note = seasonal_note(day)
    if note:
        return note
    window = (day - timedelta(days=i) for i in range(1, baseline_days + 1))
    if any(seasonal_note(d) for d in window):
        return "baseline window includes holiday-season days"
    return None


def _context_for_week(label: str) -> str | None:
    monday = week_start(label)
    notes = [seasonal_note(monday + timedelta(days=i)) for i in range(7)]
    unique = list(dict.fromkeys(n for n in notes if n))
    return "; ".join(unique) or None


# --- statistical check -------------------------------------------------------------


def robust_z(value: float, baseline: np.ndarray) -> float | None:
    """Modified z-score of ``value`` against ``baseline`` (NaNs already removed)."""
    median = float(np.median(baseline))
    deviations = np.abs(baseline - median)
    mad = float(np.median(deviations))
    if mad > 0:
        return _Z_SCALE * (value - median) / mad
    mean_ad = float(np.mean(deviations))
    if mean_ad > 0:
        return (value - median) / (_MEAN_AD_SCALE * mean_ad)
    return None


def _with_daily_total(daily: pd.DataFrame) -> pd.DataFrame:
    sums = [
        c
        for c in (
            "sessions",
            "view_item_sessions",
            "add_to_cart_sessions",
            "begin_checkout_sessions",
            "purchase_sessions",
            "purchases",
            "revenue",
            "purchases_missing_revenue",
        )
        if c in daily.columns
    ]
    total = daily.groupby("date", as_index=False)[sums].sum()
    total["channel"] = TOTAL
    total["spend"] = np.nan  # spend is per paid channel only
    return pd.concat([daily, add_ratios(total)], ignore_index=True)


def statistical_anomalies(
    daily: pd.DataFrame, cfg: AnomalyConfig, baseline_days: int
) -> list[Anomaly]:
    frame = _with_daily_total(daily)
    frame["date"] = pd.to_datetime(frame["date"])
    all_days = pd.date_range(frame["date"].min(), frame["date"].max(), freq="D")
    found: list[Anomaly] = []
    for (channel, metric), series, volume in _series(frame, cfg.metrics, all_days):
        values = series.to_numpy(dtype=float)
        volumes = None if volume is None else volume.to_numpy(dtype=float)
        for i in range(len(values)):
            x = values[i]
            if np.isnan(x):
                continue
            window = values[max(0, i - baseline_days) : i]
            window = window[~np.isnan(window)]
            if len(window) < cfg.min_baseline_days:
                continue
            if volumes is not None:
                basis = volumes[max(0, i - baseline_days) : i]
                if np.nanmedian(basis) < cfg.min_daily_volume:
                    continue
            z = robust_z(x, window)
            if z is None or abs(z) < cfg.z_threshold:
                continue
            day = all_days[i].date()
            found.append(
                Anomaly(
                    channel=str(channel),
                    metric=metric,
                    date_or_week=day.isoformat(),
                    granularity="day",
                    rule="robust_z",
                    value=x,
                    baseline=float(np.median(window)),
                    score=round(z, 2),
                    direction="up" if z > 0 else "down",
                    severity="high" if abs(z) >= 2 * cfg.z_threshold else "medium",
                    seasonal_context=_context_for_day(day, baseline_days),
                    is_simulated=metric in SPEND_BASED,
                )
            )
    return found


def _series(
    frame: pd.DataFrame, metrics: list[str], all_days: pd.DatetimeIndex
) -> list[tuple[tuple[str, str], pd.Series, pd.Series | None]]:
    out = []
    for channel, group in frame.groupby("channel"):
        indexed = group.set_index("date").reindex(all_days)
        for metric in metrics:
            if metric in indexed and indexed[metric].notna().any():
                basis = VOLUME_BASIS.get(metric)
                volume = indexed[basis] if basis else None
                out.append(((str(channel), metric), indexed[metric], volume))
    return out


# --- weekly rules --------------------------------------------------------------------


def _week_anomaly(
    row: object,
    rule: str,
    metric: str,
    value: object,
    baseline: object,
    score: object,
    direction: Literal["up", "down"],
    high: bool,
) -> Anomaly:
    week = str(row.week)  # type: ignore[attr-defined]
    rounded = _num(score)
    return Anomaly(
        channel=str(row.channel),  # type: ignore[attr-defined]
        metric=metric,
        date_or_week=week,
        granularity="week",
        rule=rule,
        value=_num(value),
        baseline=_num(baseline),
        score=None if rounded is None else round(rounded, 4),
        direction=direction,
        severity="high" if high else "medium",
        seasonal_context=_context_for_week(week),
        is_simulated=metric in SPEND_BASED,
    )


def rule_anomalies(weekly: pd.DataFrame, cfg: AnomalyConfig, paid: list[str]) -> list[Anomaly]:
    """Explicit rules on complete weeks; expects ``add_week_over_week`` output."""
    rules = cfg.rules
    found: list[Anomaly] = []
    for row in weekly[weekly["is_complete"]].itertuples(index=False):
        channel = str(row.channel)

        if channel in paid and (_num(row.spend) or 0) > 0 and row.purchases == 0:
            found.append(
                _week_anomaly(
                    row,
                    "paid_spend_no_purchases",
                    "purchases",
                    0,
                    row.prev_purchases,
                    None,
                    "down",
                    True,
                )
            )

        for metric in ("cpa", "cost_per_session"):
            change = _num(getattr(row, f"wow_{metric}"))
            if change is not None and change > rules.cost_wow_increase:
                found.append(
                    _week_anomaly(
                        row,
                        "cost_increase",
                        metric,
                        getattr(row, metric),
                        getattr(row, f"prev_{metric}"),
                        change,
                        "up",
                        change > 1.0,
                    )
                )

        cr_change = _num(row.wow_conversion_rate)
        sessions_change = _num(row.wow_sessions)
        if (
            cr_change is not None
            and sessions_change is not None
            and cr_change < -rules.conversion_rate_wow_drop
            and abs(sessions_change) <= rules.sessions_stable_max_change
        ):
            found.append(
                _week_anomaly(
                    row,
                    "conversion_drop",
                    "conversion_rate",
                    row.conversion_rate,
                    row.prev_conversion_rate,
                    cr_change,
                    "down",
                    cr_change < -0.5,
                )
            )

        shift = _num(row.revenue_share_change_pp)
        if channel != TOTAL and shift is not None and abs(shift) > rules.revenue_share_change_pp:
            found.append(
                _week_anomaly(
                    row,
                    "revenue_share_shift",
                    "revenue_share",
                    row.revenue_share,
                    row.prev_revenue_share,
                    shift,
                    "up" if shift > 0 else "down",
                    abs(shift) > 2 * rules.revenue_share_change_pp,
                )
            )

        gap = _num(row.missing_revenue_share)
        if channel == TOTAL and gap is not None and gap > rules.tracking_gap_share:
            found.append(
                _week_anomaly(
                    row, "tracking_gap", "missing_revenue_share", gap, None, gap, "up", True
                )
            )
    return found


def detect_anomalies(
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
    cfg: AnomalyConfig,
    paid: list[str],
    baseline_days: int,
) -> list[Anomaly]:
    return statistical_anomalies(daily, cfg, baseline_days) + rule_anomalies(weekly, cfg, paid)


def anomalies_for_week(anomalies: list[Anomaly], week: str) -> list[Anomaly]:
    return [a for a in anomalies if a.week == week]


def to_frame(anomalies: list[Anomaly]) -> pd.DataFrame:
    return pd.DataFrame([a.model_dump() for a in anomalies])
