"""Native FastAPI backend for OpenClips Studio.

No Docker, no database, no broker: jobs run in background threads, clips and
their state land in the project's `clips/` folder, and the UI polls for
progress.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from studio import pipeline

WEB_DIR = pipeline.PROJECT_ROOT / "web"

app = FastAPI(title="OpenClips Studio", version="2.0.0")
api = APIRouter(prefix="/api")

_running: dict[str, threading.Thread] = {}


class JobRequest(BaseModel):
    source: str
    max_clips: int = 12
    model: str = pipeline.DEFAULT_MODEL
    workers: int = 4
    face_track: bool = True
    transcript: str | None = None  # optional pre-computed transcript, for reruns


@api.post("/jobs")
def create_job(request: JobRequest, background: BackgroundTasks) -> dict[str, Any]:
    source = request.source.strip()
    if not source:
        raise HTTPException(status_code=422, detail="A URL or file path is required")

    job_id = pipeline.new_job_id()
    state = pipeline.JobState(id=job_id, source=source, status="queued")
    pipeline.write_state(state)

    def run() -> None:
        pipeline.run_job(
            job_id=job_id,
            source=source,
            hook=lambda *_: None,
            max_clips=request.max_clips,
            model_size=request.model,
            workers=request.workers,
            face_track=request.face_track,
            transcript_path=Path(request.transcript) if request.transcript else None,
        )
        _running.pop(job_id, None)

    thread = threading.Thread(target=run, daemon=True)
    _running[job_id] = thread
    thread.start()
    return {"id": job_id}


@api.get("/jobs")
def all_jobs() -> list[dict[str, Any]]:
    return [job.to_dict() for job in pipeline.list_jobs()]


@api.get("/jobs/{job_id}")
def one_job(job_id: str) -> dict[str, Any]:
    state = pipeline.read_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return state.to_dict()


@api.delete("/jobs/{job_id}")
def delete_job(job_id: str) -> dict[str, str]:
    pipeline.clear_job(job_id)
    return {"status": "deleted"}


@api.get("/jobs/{job_id}/file/{name}")
def job_file(job_id: str, name: str) -> FileResponse:
    # Resolve inside the job directory so a crafted name cannot escape it.
    directory = pipeline.job_dir(job_id).resolve()
    path = (directory / name).resolve()
    if not path.is_file() or directory not in path.parents:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


app.include_router(api)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = WEB_DIR / "index.html"
    if not page.is_file():
        return HTMLResponse("<h1>UI missing</h1>", status_code=500)
    return HTMLResponse(page.read_text())


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    serve()
