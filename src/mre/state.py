"""Scheduled-run state.

The GA4 sample is a fixed historical dataset (2020-11 to 2021-01), so "this week's
report" does not exist. The scheduled job instead walks through the dataset's complete
weeks, one per run, remembering its position in ``out/state.json`` and starting over
after the last week. This simulates a live weekly cadence; it does not pretend the
data is current.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATE_PATH = Path("out/state.json")


@dataclass(frozen=True)
class RunState:
    last_week: str | None = None
    runs: int = 0


def load_state(path: Path) -> RunState:
    if not path.exists():
        return RunState()
    data = json.loads(path.read_text(encoding="utf-8"))
    return RunState(last_week=data.get("last_week"), runs=int(data.get("runs", 0)))


def save_state(path: Path, state: RunState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_week": state.last_week, "runs": state.runs}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def next_week(weeks: list[str], state: RunState) -> str:
    """The week after ``state.last_week``; the first week when starting or wrapping."""
    if not weeks:
        raise ValueError("No complete weeks in the data")
    if state.last_week in weeks:
        i = weeks.index(state.last_week)
        return weeks[(i + 1) % len(weeks)]
    return weeks[0]
