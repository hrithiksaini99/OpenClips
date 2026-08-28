"""End-to-end pipeline: source in, postable vertical clips out.

Runs natively (no Docker, no database). State for a job lives in a single JSON
file beside its clips, which is enough for a local studio tool and keeps the
whole thing inspectable from Finder.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from studio.captions import CaptionStyle
from studio.render import render_clip
from studio.select import ClipCandidate, find_clips
from studio.transcript import Word, build_sentences, load_words

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLIPS_DIR = PROJECT_ROOT / "clips"
MEDIA_DIR = PROJECT_ROOT / "media"
DEFAULT_MODEL = "small"

ProgressHook = Callable[[str, float, str], None]


@dataclass
class ClipRecord:
    id: str
    title: str
    start: float
    end: float
    duration: float
    score: float
    text: str
    file: str
    thumbnail: str = ""


@dataclass
class JobState:
    id: str
    source: str
    title: str = ""
    status: str = "queued"
    stage: str = "queued"
    progress: float = 0.0
    message: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    clips: list[ClipRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["clips"] = [asdict(clip) for clip in self.clips]
        return payload


def _slug(text: str, limit: int = 48) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (cleaned[:limit] or "clip").rstrip("-")


def job_dir(job_id: str) -> Path:
    return CLIPS_DIR / job_id


def write_state(state: JobState) -> None:
    directory = job_dir(state.id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "job.json").write_text(json.dumps(state.to_dict(), indent=2))


def read_state(job_id: str) -> JobState | None:
    path = job_dir(job_id) / "job.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text())
    clips = [ClipRecord(**clip) for clip in payload.pop("clips", [])]
    return JobState(**payload, clips=clips)


def list_jobs() -> list[JobState]:
    if not CLIPS_DIR.is_dir():
        return []
    jobs = [read_state(path.name) for path in CLIPS_DIR.iterdir() if path.is_dir()]
    found = [job for job in jobs if job is not None]
    found.sort(key=lambda job: job.created_at, reverse=True)
    return found


def download(url: str, destination: Path, hook: ProgressHook) -> Path:
    """Fetch a single video with yt-dlp, best quality merged to mp4."""
    hook("download", 0.05, "Downloading source video…")
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "yt-dlp", "--no-progress", "--newline",
            "-f", "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]",
            "--merge-output-format", "mp4",
            "-o", str(destination), "--", url,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=7200,
    )
    return destination


def transcribe(video: Path, model_size: str, hook: ProgressHook) -> list[Word]:
    """Transcribe with faster-whisper, streaming progress as segments arrive."""
    from faster_whisper import WhisperModel

    hook("transcribe", 0.15, f"Loading {model_size} speech model…")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(video), word_timestamps=True, vad_filter=True, beam_size=1,
    )
    total = float(info.duration) or 1.0
    words: list[Word] = []
    for segment in segments:
        for raw in segment.words or []:
            text = str(raw.word).strip()
            if text:
                words.append(
                    Word(
                        text=text,
                        start=float(raw.start),
                        end=max(float(raw.start), float(raw.end)),
                        probability=float(getattr(raw, "probability", 1.0)),
                    )
                )
        share = min(segment.end / total, 1.0)
        hook("transcribe", 0.15 + share * 0.45, f"Transcribing… {share * 100:.0f}%")
    return words


def _render_one(payload: dict[str, Any]) -> dict[str, Any]:
    """Worker entry point: render a single clip in its own process."""
    words = [Word(**word) for word in payload["words"]]
    render_clip(
        source=Path(payload["source"]),
        destination=Path(payload["destination"]),
        start=payload["start"],
        end=payload["end"],
        words=words,
        style=CaptionStyle(**payload["style"]),
        face_track=payload["face_track"],
    )
    thumbnail = Path(payload["destination"]).with_suffix(".jpg")
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-ss", "1", "-i", payload["destination"],
            "-frames:v", "1", "-vf", "scale=360:-2", str(thumbnail),
        ],
        check=False,
        capture_output=True,
        timeout=120,
    )
    return {"id": payload["id"], "thumbnail": thumbnail.name}


def run_job(
    *,
    job_id: str,
    source: str,
    hook: ProgressHook,
    max_clips: int = 12,
    model_size: str = DEFAULT_MODEL,
    workers: int = 4,
    transcript_path: Path | None = None,
    face_track: bool = True,
) -> JobState:
    """Take a URL or local file all the way to rendered clips on disk."""
    directory = job_dir(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    state = JobState(id=job_id, source=source, status="running")

    def report(stage: str, progress: float, message: str) -> None:
        state.stage, state.progress, state.message = stage, progress, message
        write_state(state)
        hook(stage, progress, message)

    try:
        if source.startswith(("http://", "https://")):
            video = download(source, MEDIA_DIR / "source" / f"{job_id}.mp4", report)
        else:
            video = Path(source).expanduser().resolve()
            if not video.is_file():
                raise FileNotFoundError(f"Source file not found: {video}")
        state.title = video.stem

        if transcript_path is not None:
            report("transcribe", 0.55, "Loading existing transcript…")
            words = load_words(transcript_path)
        else:
            words = transcribe(video, model_size, report)
        if not words:
            raise RuntimeError("Transcription produced no words")

        report("select", 0.62, "Finding the strongest moments…")
        candidates = find_clips(build_sentences(words), max_clips=max_clips)
        if not candidates:
            raise RuntimeError("No clip-worthy segments found")

        report("render", 0.68, f"Rendering {len(candidates)} clips in parallel…")
        payloads = []
        records: dict[str, ClipRecord] = {}
        for index, candidate in enumerate(candidates, start=1):
            clip_id = f"{index:02d}-{_slug(candidate.title)}"
            destination = directory / f"{clip_id}.mp4"
            window = [
                asdict(word)
                for word in words
                if word.end > candidate.start and word.start < candidate.end
            ]
            payloads.append(
                {
                    "id": clip_id,
                    "source": str(video),
                    "destination": str(destination),
                    "start": candidate.start,
                    "end": candidate.end,
                    "words": window,
                    "style": asdict(CaptionStyle()),
                    "face_track": face_track,
                }
            )
            records[clip_id] = ClipRecord(
                id=clip_id,
                title=candidate.title,
                start=candidate.start,
                end=candidate.end,
                duration=candidate.duration,
                score=candidate.score,
                text=candidate.text,
                file=destination.name,
            )

        done = 0
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_render_one, payload) for payload in payloads]
            for future in as_completed(futures):
                result = future.result()
                records[result["id"]].thumbnail = result["thumbnail"]
                done += 1
                report(
                    "render",
                    0.68 + 0.3 * (done / len(payloads)),
                    f"Rendered {done}/{len(payloads)} clips",
                )

        state.clips = [records[payload["id"]] for payload in payloads]
        state.status = "done"
        report("done", 1.0, f"{len(state.clips)} clips ready")
    except Exception as error:  # surface the failure in the UI rather than dying silently
        state.status = "error"
        state.error = f"{type(error).__name__}: {error}"
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            state.error += f" | {str(error.stderr)[-400:]}"
        report("error", state.progress, state.error)
    write_state(state)
    return state


def clear_job(job_id: str) -> None:
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


def new_job_id() -> str:
    return uuid.uuid4().hex[:10]
