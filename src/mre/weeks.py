"""ISO week helpers.

Weeks are ISO 8601 (Monday to Sunday). 2020 has a week 53, so 2020-W53 runs from
2020-12-28 to 2021-01-03 and the week after it is 2021-W01.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

_LABEL = re.compile(r"^(\d{4})-W(\d{2})$")


def iso_week_label(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_start(label: str) -> date:
    """Monday of an ISO week label such as ``2020-W53``."""
    match = _LABEL.match(label)
    if not match:
        raise ValueError(f"Not an ISO week label: {label!r}")
    year, week = int(match.group(1)), int(match.group(2))
    return date.fromisocalendar(year, week, 1)  # raises for week 53 in 52-week years


def previous_week(label: str) -> str:
    return iso_week_label(week_start(label) - timedelta(days=7))
