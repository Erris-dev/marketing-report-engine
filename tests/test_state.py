from pathlib import Path

import pytest
from typer.testing import CliRunner

from mre.cli import app
from mre.state import RunState, load_state, next_week, save_state

WEEKS = ["2020-W52", "2020-W53", "2021-W01"]


def test_first_run_starts_at_first_week() -> None:
    assert next_week(WEEKS, RunState()) == "2020-W52"


def test_advances_across_year_boundary_and_wraps() -> None:
    assert next_week(WEEKS, RunState(last_week="2020-W53")) == "2021-W01"
    assert next_week(WEEKS, RunState(last_week="2021-W01")) == "2020-W52"


def test_unknown_last_week_restarts() -> None:
    assert next_week(WEEKS, RunState(last_week="1999-W01")) == "2020-W52"


def test_no_weeks_is_an_error() -> None:
    with pytest.raises(ValueError, match="No complete weeks"):
        next_week([], RunState())


def test_state_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "out" / "state.json"
    assert load_state(path) == RunState()
    save_state(path, RunState(last_week="2020-W53", runs=4))
    assert load_state(path) == RunState(last_week="2020-W53", runs=4)


def test_scheduled_command_advances_state(workdir: Path) -> None:
    runner = CliRunner()
    state = workdir / "out" / "state.json"
    for expected in ("2020-W52", "2020-W53"):
        result = runner.invoke(app, ["scheduled", "--no-deliver"])
        if result.exit_code != 0 and "WeasyPrint" in str(result.exception):
            pytest.skip("PDF rendering needs WeasyPrint (runs in Docker/CI)")
        assert result.exit_code == 0, result.output
        assert f"reporting {expected}" in result.output
        assert load_state(state).last_week == expected
