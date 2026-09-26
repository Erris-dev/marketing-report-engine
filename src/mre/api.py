"""HTTP API: request a weekly report and download the PDF.

``POST /reports {"week": "2020-W48"}`` returns the PDF when it already exists;
otherwise it starts a background job and answers ``202`` with a status URL.
``GET /reports/{week}`` then returns the PDF, or the job status while it runs.

Jobs live in memory: this is a single-process service for local or internal use,
without authentication, so it binds to 127.0.0.1 by default.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from mre.config import ISO_WEEK_PATTERN, AppConfig, load_config, load_secrets
from mre.metrics import complete_weeks
from mre.pipeline import Prepared, prepare
from mre.render import ReportPaths, render_report

log = logging.getLogger(__name__)

Renderer = Callable[..., ReportPaths]


class ReportRequest(BaseModel):
    week: str = Field(pattern=ISO_WEEK_PATTERN, examples=["2020-W48"])


class JobStatus(BaseModel):
    week: str
    status: Literal["queued", "running", "done", "failed"]
    status_url: str
    error: str | None = None


@dataclass
class _State:
    cfg: AppConfig
    api_key: str | None
    out_dir: Path
    renderer: Renderer
    prepared: Prepared | None = None
    jobs: dict[str, JobStatus] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def get_prepared(self) -> Prepared:
        with self.lock:
            if self.prepared is None:
                self.prepared = prepare(self.cfg)
            return self.prepared


def create_app(
    cfg: AppConfig | None = None,
    *,
    api_key: str | None = None,
    out_dir: Path = Path("out/reports"),
    renderer: Renderer = render_report,
    prepared: Prepared | None = None,
) -> FastAPI:
    """App factory; tests inject a config, a fake renderer and prepared data."""
    if cfg is None:
        cfg = load_config()
        key = load_secrets().openrouter_api_key
        api_key = key.get_secret_value() if key else None
    state = _State(cfg, api_key, out_dir, renderer, prepared)
    app = FastAPI(title="Marketing Report Engine", version="0.1.0")

    def pdf_path(week: str) -> Path:
        return state.out_dir / f"{week}.pdf"

    def status_url(week: str) -> str:
        return f"/reports/{week}"

    def run_job(week: str) -> None:
        job = state.jobs[week]
        state.jobs[week] = job.model_copy(update={"status": "running"})
        try:
            state.renderer(
                week, state.cfg, state.api_key, state.out_dir, prepared=state.get_prepared()
            )
        except Exception as exc:  # report the failure through the status endpoint
            log.exception("Report job for %s failed", week)
            state.jobs[week] = job.model_copy(
                update={"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            )
            return
        state.jobs[week] = job.model_copy(update={"status": "done"})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/weeks")
    def weeks() -> dict[str, list[str]]:
        return {"weeks": complete_weeks(state.get_prepared().weekly)}

    @app.post("/reports", response_model=None)
    def create_report(request: ReportRequest, background: BackgroundTasks) -> Any:
        week = request.week
        if pdf_path(week).exists():
            return FileResponse(
                pdf_path(week), media_type="application/pdf", filename=f"{week}.pdf"
            )
        if week not in complete_weeks(state.get_prepared().weekly):
            raise HTTPException(404, f"{week} is not a complete week in the data")
        existing = state.jobs.get(week)
        if existing and existing.status in ("queued", "running"):
            return JSONResponse(existing.model_dump(), status_code=202)
        job = JobStatus(week=week, status="queued", status_url=status_url(week))
        state.jobs[week] = job
        background.add_task(run_job, week)
        return JSONResponse(job.model_dump(), status_code=202)

    @app.get("/reports/{week}", response_model=None)
    def get_report(week: str) -> Any:
        if pdf_path(week).exists():
            return FileResponse(
                pdf_path(week), media_type="application/pdf", filename=f"{week}.pdf"
            )
        job = state.jobs.get(week)
        if job is None:
            raise HTTPException(404, f"No report or job for {week}; POST /reports first")
        code = 500 if job.status == "failed" else 202
        return JSONResponse(job.model_dump(), status_code=code)

    return app
