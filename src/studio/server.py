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
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from studio import pipeline, publisher, youtube

WEB_DIR = pipeline.PROJECT_ROOT / "web"

PRIVACY_LEVELS = {"private", "unlisted", "public"}
_SLOT = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The scheduler only posts when it is armed in the UI, which it never is by
    # default; starting the thread here just means an armed schedule survives a
    # restart without anyone having to press anything.
    publisher.scheduler.start()
    yield
    publisher.scheduler.stop()


app = FastAPI(title="OpenClips Studio", version="2.1.0", lifespan=lifespan)
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
        finished = pipeline.run_job(
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
        # Writing a post calls the model once per clip, so it happens here on
        # the job thread rather than holding up the request that started it.
        if finished.status == "done" and publisher.load().schedule.auto_enqueue:
            try:
                publisher.enqueue(job_id)
            except Exception as error:
                publisher.note_error(f"Could not queue clips: {error}")

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



# --------------------------------------------------------------------------
# YouTube account
# --------------------------------------------------------------------------


def _redirect_uri(request: Request) -> str:
    """Where Google sends the browser back.

    The host is pinned to 127.0.0.1 rather than read from the browser: Google
    requires a loopback IP for desktop OAuth clients, and someone browsing to
    "localhost:8080" would otherwise produce a redirect_uri mismatch.
    """
    return f"http://127.0.0.1:{request.url.port or 8080}/api/youtube/callback"


def _callback_page(message: str, *, ok: bool) -> HTMLResponse:
    """The little page Google's redirect lands on."""
    tone = "#7FB069" if ok else "#C2554D"
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8"><title>OpenClips</title>
<body style="margin:0;display:grid;place-items:center;height:100vh;
background:#0A0A0C;color:#ECECEE;font:15px/1.6 system-ui,sans-serif">
<div style="max-width:34rem;padding:2rem;text-align:center">
<div style="width:.5rem;height:.5rem;border-radius:50%;background:{tone};
margin:0 auto 1.25rem"></div>
<p style="margin:0">{message}</p>
<p style="margin:1rem 0 0;color:#8A8A94;font-size:13px">
Return to the OpenClips tab.</p></div>""",
        status_code=200 if ok else 400,
    )


class ClientFile(BaseModel):
    """The contents of the OAuth JSON Google hands you, pasted or dropped in."""

    payload: dict[str, Any] | None = None
    path: str | None = None


@api.get("/youtube/status")
def youtube_status() -> dict[str, Any]:
    return youtube.status()


@api.post("/youtube/client")
def youtube_set_client(request: ClientFile) -> dict[str, Any]:
    """Take the OAuth client file, so nobody has to move it into config/ by hand."""
    try:
        if request.path:
            youtube.adopt_client(request.path)
        elif request.payload:
            youtube.save_client(request.payload)
        else:
            raise HTTPException(status_code=422, detail="Give a file or a path")
    except youtube.SetupRequired as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return youtube.status()


@api.get("/youtube/auth/start")
def youtube_auth_start(request: Request) -> dict[str, str]:
    try:
        return {"url": youtube.begin(_redirect_uri(request))}
    except youtube.SetupRequired as error:
        # 428: the request is fine, but a prerequisite has not been done yet.
        raise HTTPException(status_code=428, detail=str(error)) from None


@api.get("/youtube/callback", response_class=HTMLResponse)
def youtube_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> HTMLResponse:
    if error:
        return _callback_page(f"YouTube declined the request: {error}", ok=False)
    if not code or not state:
        return _callback_page("That sign-in came back incomplete.", ok=False)
    try:
        youtube.complete(code=code, state=state, redirect_uri=_redirect_uri(request))
    except youtube.YouTubeError as failure:
        return _callback_page(str(failure), ok=False)
    # Arming here is what "connect and start posting" means: the account was
    # just attached on purpose, and the schedule is the reason it was attached.
    publisher.configure({"enabled": True})
    return _callback_page("Account connected. Posting is on.", ok=True)


@api.delete("/youtube/session")
def youtube_disconnect() -> dict[str, str]:
    youtube.disconnect()
    return {"status": "disconnected"}


# --------------------------------------------------------------------------
# Schedule and publishing queue
# --------------------------------------------------------------------------


class ScheduleUpdate(BaseModel):
    enabled: bool | None = None
    slots: list[str] | None = None
    privacy: str | None = None
    category_id: str | None = None
    made_for_kids: bool | None = None
    auto_enqueue: bool | None = None
    daily_limit: int | None = None


class EnqueueRequest(BaseModel):
    job_id: str
    clip_ids: list[str] | None = None


class EntryUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    hashtags: list[str] | None = None
    privacy: str | None = None


@api.get("/schedule")
def get_schedule() -> dict[str, Any]:
    return asdict(publisher.load().schedule)


@api.put("/schedule")
def put_schedule(update: ScheduleUpdate) -> dict[str, Any]:
    changes = update.model_dump(exclude_none=True)
    if "privacy" in changes and changes["privacy"] not in PRIVACY_LEVELS:
        raise HTTPException(status_code=422, detail="privacy must be private, unlisted or public")
    if "slots" in changes:
        # Silently dropping a malformed time would leave the user with a
        # schedule that looks armed and never fires, so reject the whole update.
        cleaned = [slot.strip() for slot in changes["slots"]]
        bad = [slot for slot in cleaned if not _SLOT.match(slot)]
        if bad:
            raise HTTPException(status_code=422, detail=f"Not a time of day: {', '.join(bad)}")
        if not cleaned:
            raise HTTPException(status_code=422, detail="Give at least one time of day")
        changes["slots"] = sorted(set(cleaned))
    if "daily_limit" in changes:
        changes["daily_limit"] = max(1, min(int(changes["daily_limit"]), publisher.DAILY_CAP))
    return asdict(publisher.configure(changes))


@api.get("/queue")
def get_queue() -> dict[str, Any]:
    board = publisher.load()
    times = publisher.upcoming(board)
    return {
        "entries": [
            {**asdict(entry), "scheduled_for": times.get(entry.id, "")}
            for entry in board.queue
        ],
        "schedule": asdict(board.schedule),
        "posted_today": publisher.posted_today(board.queue, datetime.now()),
        "daily_limit": min(board.schedule.daily_limit, publisher.DAILY_CAP),
        "last_error": board.last_error,
    }


@api.post("/queue")
def post_queue(request: EnqueueRequest, background: BackgroundTasks) -> dict[str, str]:
    if pipeline.read_state(request.job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # One model call per clip; the queue fills in behind the next few polls.
    background.add_task(publisher.enqueue, request.job_id, request.clip_ids)
    return {"status": "writing"}


@api.patch("/queue/{entry_id}")
def patch_queue(entry_id: str, update: EntryUpdate) -> dict[str, Any]:
    changes = update.model_dump(exclude_none=True)
    if "privacy" in changes and changes["privacy"] not in PRIVACY_LEVELS:
        raise HTTPException(status_code=422, detail="privacy must be private, unlisted or public")
    entry = publisher.edit(entry_id, changes)
    if entry is None:
        raise HTTPException(status_code=404, detail="No editable entry with that id")
    return asdict(entry)


@api.delete("/queue/{entry_id}")
def delete_queue(entry_id: str) -> dict[str, str]:
    if not publisher.remove(entry_id):
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"status": "removed"}


@api.post("/queue/{entry_id}/publish")
def publish_now(entry_id: str, background: BackgroundTasks) -> dict[str, Any]:
    """Post one clip straight away, without waiting for its slot."""
    try:
        entry = publisher.claim_now(entry_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Entry not found") from None
    except youtube.NotConnected as error:
        raise HTTPException(status_code=428, detail=str(error)) from None
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    # The upload takes as long as it takes; the queue shows it as uploading.
    background.add_task(publisher.scheduler.post, entry_id)
    return asdict(entry)


@api.post("/queue/{entry_id}/retry")
def retry_queue(entry_id: str) -> dict[str, Any]:
    entry = publisher.retry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No failed entry with that id")
    return asdict(entry)


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
