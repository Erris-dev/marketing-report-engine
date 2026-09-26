from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mre.api import create_app
from mre.config import AppConfig
from mre.narrative import NarrativeOutput, NarrativeResult
from mre.pipeline import Prepared
from mre.render import ReportPaths

NARRATIVE = NarrativeResult(
    NarrativeOutput(summary="A. B. C.", findings=["a", "b", "c"], checks=["d", "e", "f"]),
    "fallback",
)


class FakeRenderer:
    """Stands in for render_report: writes a tiny PDF, or fails on demand."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def __call__(self, week: str, cfg: Any, key: Any, out_dir: Path, **kw: Any) -> ReportPaths:
        self.calls.append(week)
        if self.fail:
            raise RuntimeError("renderer exploded")
        out_dir.mkdir(parents=True, exist_ok=True)
        pdf = out_dir / f"{week}.pdf"
        pdf.write_bytes(b"%PDF-1.7 test")
        return ReportPaths(html=out_dir / f"{week}.html", pdf=pdf, narrative=NARRATIVE)


def _client(
    tmp_path: Path, config: AppConfig, prepared: Prepared, renderer: FakeRenderer
) -> TestClient:
    app = create_app(config, out_dir=tmp_path / "reports", renderer=renderer, prepared=prepared)
    return TestClient(app)


def test_health(tmp_path: Path, config: AppConfig, prepared: Prepared) -> None:
    client = _client(tmp_path, config, prepared, FakeRenderer())
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/weeks").json() == {"weeks": ["2020-W52", "2020-W53", "2021-W01"]}


def test_post_starts_job_then_pdf_is_served(
    tmp_path: Path, config: AppConfig, prepared: Prepared
) -> None:
    renderer = FakeRenderer()
    client = _client(tmp_path, config, prepared, renderer)

    first = client.post("/reports", json={"week": "2021-W01"})
    assert first.status_code == 202
    assert first.json()["status"] == "queued"
    assert first.json()["status_url"] == "/reports/2021-W01"
    # TestClient runs background tasks before returning, so the job has finished.
    assert renderer.calls == ["2021-W01"]

    pdf = client.get("/reports/2021-W01")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")

    again = client.post("/reports", json={"week": "2021-W01"})
    assert again.status_code == 200  # existing PDF returned directly
    assert renderer.calls == ["2021-W01"]  # not rendered twice


@pytest.mark.parametrize("week", ["2020-48", "2020-W54", "W48", ""])
def test_invalid_week_is_rejected(
    tmp_path: Path, config: AppConfig, prepared: Prepared, week: str
) -> None:
    client = _client(tmp_path, config, prepared, FakeRenderer())
    assert client.post("/reports", json={"week": week}).status_code == 422


def test_week_outside_data_is_404(tmp_path: Path, config: AppConfig, prepared: Prepared) -> None:
    client = _client(tmp_path, config, prepared, FakeRenderer())
    assert client.post("/reports", json={"week": "2021-W30"}).status_code == 404
    assert client.get("/reports/2021-W30").status_code == 404


def test_failed_job_reports_error(tmp_path: Path, config: AppConfig, prepared: Prepared) -> None:
    client = _client(tmp_path, config, prepared, FakeRenderer(fail=True))
    assert client.post("/reports", json={"week": "2021-W01"}).status_code == 202
    status = client.get("/reports/2021-W01")
    assert status.status_code == 500
    assert status.json()["status"] == "failed"
    assert "renderer exploded" in status.json()["error"]
