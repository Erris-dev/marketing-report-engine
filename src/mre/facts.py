"""Build ``facts.json``: the only thing the LLM ever sees.

Every number is already computed and rounded here, so the number guard can compare the
LLM's text with exactly these values. Rates are stored as percentages (12.3, not 0.123)
because that is how prose writes them. Output is deterministic: same inputs, same bytes.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from typing import Any

import pandas as pd

from mre.anomalies import Anomaly, anomalies_for_week
from mre.config import AppConfig
from mre.metrics import TOTAL
from mre.weeks import previous_week, week_start

SCHEMA_VERSION = 1
UNKNOWN_CHANNEL = "unknown"
PERCENT_METRICS = frozenset(
    {
        "conversion_rate",
        "revenue_share",
        "session_share",
        "missing_revenue_share",
        "add_to_cart_rate",
        "checkout_rate",
        "purchase_rate",
    }
)
MONEY_METRICS = frozenset({"revenue", "spend", "aov", "cpa", "cost_per_session"})


def _clean(value: Any) -> float | None:
    if value is None:
        return None
    f = float(value)
    return None if math.isnan(f) or math.isinf(f) else f


def _round(value: Any, digits: int) -> float | None:
    f = _clean(value)
    return None if f is None else round(f, digits)


def _int(value: Any) -> int | None:
    f = _clean(value)
    return None if f is None else round(f)


def _pct(value: Any) -> float | None:
    """Ratio -> percentage with one decimal (0.1234 -> 12.3)."""
    f = _clean(value)
    return None if f is None else round(f * 100, 1)


def _metric_value(metric: str, value: Any) -> float | None:
    if metric in PERCENT_METRICS:
        return _pct(value)
    if metric in MONEY_METRICS:
        return _round(value, 2)
    return _round(value, 2)


def _channel_facts(row: pd.Series, paid: bool) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "channel": row["channel"],
        "is_paid": paid,
        "sessions": _int(row["sessions"]),
        "purchases": _int(row["purchases"]),
        "revenue": _round(row["revenue"], 2),
        "conversion_rate_pct": _pct(row["conversion_rate"]),
        "aov": _round(row["aov"], 2),
        "session_share_pct": _pct(row["session_share"]),
        "revenue_share_pct": _pct(row["revenue_share"]),
        "wow_sessions_pct": _pct(row["wow_sessions"]),
        "wow_purchases_pct": _pct(row["wow_purchases"]),
        "wow_revenue_pct": _pct(row["wow_revenue"]),
        "wow_conversion_rate_pct": _pct(row["wow_conversion_rate"]),
        "revenue_share_change_pp": _round(row["revenue_share_change_pp"], 1),
    }
    if paid:
        facts["simulated_spend"] = {
            "spend": _round(row["spend"], 2),
            "cost_per_session": _round(row["cost_per_session"], 2),
            "cpa": _round(row["cpa"], 2),
            "roas": _round(row["roas"], 2),
            "wow_spend_pct": _pct(row["wow_spend"]),
            "wow_cpa_pct": _pct(row["wow_cpa"]),
            "wow_cost_per_session_pct": _pct(row["wow_cost_per_session"]),
        }
    return facts


def _anomaly_facts(a: Anomaly) -> dict[str, Any]:
    fact: dict[str, Any] = {
        "channel": a.channel,
        "metric": a.metric,
        "when": a.date_or_week,
        "rule": a.rule,
        "value": _metric_value(a.metric, a.value),
        "baseline": _metric_value(a.metric, a.baseline),
        "unit": "%" if a.metric in PERCENT_METRICS else None,
        "direction": a.direction,
        "severity": a.severity,
        "seasonal_context": a.seasonal_context,
        "simulated": a.is_simulated,
    }
    if a.rule == "robust_z":
        fact["z_score"] = _round(a.score, 1)
    elif a.rule == "revenue_share_shift":
        fact["change_pp"] = _round(a.score, 1)
    elif a.rule in ("cost_increase", "conversion_drop"):
        fact["change_pct"] = _pct(a.score)
    return fact


def _severity_order(a: Anomaly) -> tuple[int, int, float, str, str, str]:
    # Weekly rule findings summarise the week, so they come before daily outliers;
    # z-scores and rule scores are on different scales and only compared within a group.
    return (
        0 if a.severity == "high" else 1,
        0 if a.granularity == "week" else 1,
        -abs(a.score or 0.0),
        a.date_or_week,
        a.channel,
        a.metric,
    )


def _top_movers(channels: pd.DataFrame, limit: int = 3) -> list[dict[str, Any]]:
    movers = channels.dropna(subset=["prev_revenue"]).copy()
    movers["abs_change"] = (movers["revenue"] - movers["prev_revenue"]).abs()
    movers = movers.sort_values(["abs_change", "channel"], ascending=[False, True]).head(limit)
    return [
        {
            "channel": r["channel"],
            "metric": "revenue",
            "from": _round(r["prev_revenue"], 2),
            "to": _round(r["revenue"], 2),
            "change": _round(r["revenue"] - r["prev_revenue"], 2),
            "change_pct": _pct(r["wow_revenue"]),
        }
        for _, r in movers.iterrows()
    ]


def build_facts(
    week: str,
    weekly: pd.DataFrame,
    anomalies: list[Anomaly],
    quarantined_rows: int,
    cfg: AppConfig,
) -> dict[str, Any]:
    rows = weekly[weekly["week"] == week]
    if rows.empty:
        raise ValueError(f"No data for week {week}")
    total = rows[rows["channel"] == TOTAL].iloc[0]
    if not bool(total["is_complete"]):
        raise ValueError(f"{week} is a partial week ({int(total['days'])} days); not reported")

    channels = rows[rows["channel"] != TOTAL].sort_values(
        ["revenue", "channel"], ascending=[False, True]
    )
    paid = set(cfg.channels.paid)
    unknown = channels[channels["channel"] == UNKNOWN_CHANNEL]
    week_anomalies = sorted(anomalies_for_week(anomalies, week), key=_severity_order)
    has_previous = not math.isnan(float(total["prev_sessions"]))
    start = week_start(week)

    return {
        "schema_version": SCHEMA_VERSION,
        "report_week": week,
        "week_start": start.isoformat(),
        "week_end": (start + timedelta(days=6)).isoformat(),
        "previous_week": previous_week(week) if has_previous else None,
        "currency": cfg.report.currency,
        "simulated_spend": True,
        "totals": {
            "sessions": _int(total["sessions"]),
            "purchases": _int(total["purchases"]),
            "revenue": _round(total["revenue"], 2),
            "conversion_rate_pct": _pct(total["conversion_rate"]),
            "aov": _round(total["aov"], 2),
            "wow_sessions_pct": _pct(total["wow_sessions"]),
            "wow_purchases_pct": _pct(total["wow_purchases"]),
            "wow_revenue_pct": _pct(total["wow_revenue"]),
            "wow_conversion_rate_pct": _pct(total["wow_conversion_rate"]),
        },
        "funnel_pct": {
            "view_item_to_add_to_cart": _pct(total["add_to_cart_rate"]),
            "add_to_cart_to_checkout": _pct(total["checkout_rate"]),
            "checkout_to_purchase": _pct(total["purchase_rate"]),
            "counted_as": "sessions",
            "note": (
                "A step can exceed the previous one because a session can start checkout "
                "with a cart filled in an earlier session."
            ),
        },
        "channels": [_channel_facts(r, r["channel"] in paid) for _, r in channels.iterrows()],
        "top_movers": _top_movers(channels),
        "anomaly_count": len(week_anomalies),
        "anomalies": [_anomaly_facts(a) for a in week_anomalies[: cfg.report.max_anomalies]],
        "data_quality": {
            "rows_quarantined": quarantined_rows,
            "days_covered": _int(total["days"]),
            "unknown_channel_session_share_pct": (
                _pct(unknown.iloc[0]["session_share"]) if not unknown.empty else 0.0
            ),
            "purchases_missing_revenue": _int(total["purchases_missing_revenue"]),
            "missing_revenue_share_pct": _pct(total["missing_revenue_share"]),
            "duplicate_purchase_events": _int(total["duplicate_purchase_events"]),
        },
    }


def canonical_json(facts: dict[str, Any]) -> str:
    return json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def facts_hash(facts: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(facts).encode("utf-8")).hexdigest()
