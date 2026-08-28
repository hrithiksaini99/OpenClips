"""Native FastAPI backend for OpenClips Studio.

No Docker, no database, no broker: jobs run in background threads, clips and
their state land in the project's `clips/` folder, and the UI polls for
progress.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import re
import zipfile
from collections.abc import Iterator

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
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
    use_llm: bool = True
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
            use_llm=request.use_llm,
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



class _ZipBuffer:
    """A write-only sink so the archive can be streamed, never held in memory.

    zipfile decides whether it may seek by probing for a `seek` attribute; this
    object deliberately exposes only `write`/`tell`/`flush`, so the archive is
    produced strictly forward and each finished chunk can be handed straight to
    the client. A job of twenty clips is a few hundred megabytes, which is not
    something to buffer or spool to disk just to serve a download.
    """

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._offset = 0

    def write(self, data: bytes) -> int:
        self._chunks.append(bytes(data))
        self._offset += len(data)
        return len(data)

    def tell(self) -> int:
        return self._offset

    def flush(self) -> None:
        return None

    def drain(self) -> bytes:
        chunk = b"".join(self._chunks)
        self._chunks.clear()
        return chunk


def _zip_stream(members: list[tuple[str, Path]]) -> Iterator[bytes]:
    """Yield a ZIP of the given files, stored uncompressed.

    MP4 is already compressed, so deflating it costs CPU for almost no saving.
    """
    buffer = _ZipBuffer()
    archive = zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED, allowZip64=True)
    for name, path in members:
        with archive.open(name, "w") as entry, path.open("rb") as source:
            while block := source.read(1 << 20):
                entry.write(block)
                if chunk := buffer.drain():
                    yield chunk
        if chunk := buffer.drain():
            yield chunk
    archive.close()
    if chunk := buffer.drain():
        yield chunk


def _archive_name(state: pipeline.JobState) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "-", state.title or "openclips").strip("-").lower()
    return f"{stem[:60] or 'openclips'}-clips.zip"


@api.get("/jobs/{job_id}/download")
def download_all(job_id: str) -> StreamingResponse:
    """Stream every rendered clip in one job as a single ZIP."""
    state = pipeline.read_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Job not found")
    directory = pipeline.job_dir(job_id)
    members: list[tuple[str, Path]] = []
    for index, clip in enumerate(state.clips, start=1):
        path = directory / clip.file
        if path.is_file():
            members.append((f"{index:02d}-{path.name.split('-', 1)[-1]}", path))
    if not members:
        raise HTTPException(status_code=404, detail="This job has no rendered clips")
    return StreamingResponse(
        _zip_stream(members),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{_archive_name(state)}"'},
    )


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
