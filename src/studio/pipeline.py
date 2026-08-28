"""End-to-end pipeline: source in, postable vertical clips out.

Runs natively (no Docker, no database). State for a job lives in a single JSON
file beside its clips, which is enough for a local studio tool and keeps the
whole thing inspectable from Finder.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from studio.captions import CaptionStyle
from studio.tools import binary
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
    """Persist job state atomically.

    The worker thread rewrites this file several times a second while the UI
    polls it. A plain write_text let a reader observe a truncated file, which
    surfaced as a 500 from /api/jobs and killed the browser's polling loop.
    Writing to a sibling temp file and renaming makes the swap atomic.
    """
    directory = job_dir(state.id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "job.json"
    temporary = directory / f".job.json.{uuid.uuid4().hex[:8]}"
    temporary.write_text(json.dumps(state.to_dict(), indent=2))
    os.replace(temporary, target)


def read_state(job_id: str) -> JobState | None:
    path = job_dir(job_id) / "job.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None  # a writer was mid-swap; the next poll will see it
    clips = [ClipRecord(**clip) for clip in payload.pop("clips", [])]
    return JobState(**payload, clips=clips)


def list_jobs() -> list[JobState]:
    if not CLIPS_DIR.is_dir():
        return []
    jobs = [read_state(path.name) for path in CLIPS_DIR.iterdir() if path.is_dir()]
    found = [job for job in jobs if job is not None]
    found.sort(key=lambda job: job.created_at, reverse=True)
    return found


# A bare "youtube.com/watch?v=…" is a URL a user pasted without the scheme, not
# a relative file path; treating it as a path produced a confusing "file not
# found" against the project directory.
_URL_LIKE = re.compile(r"^(?:https?://)?(?:[\w-]+\.)*(?:youtube\.com|youtu\.be)/", re.IGNORECASE)
# Channel, user and playlist locators expand to every video they contain, which
# yt-dlp will happily download for hours; they must be resolved to one episode.
_COLLECTION_MARKERS = ("/@", "/channel/", "/c/", "/user/", "/playlist", "/videos", "/streams")
_PROGRESS = re.compile(
    # The tilde is kept: until the last fragments arrive yt-dlp only knows an
    # approximate total, so it drifts (2.76 -> 2.12 GiB). Showing "~2.1GiB"
    # says that plainly instead of looking like a wrong number.
    r"\[download\]\s+([\d.]+)%(?:\s+of\s+(~?\s*\S+))?(?:\s+at\s+(\S+))?(?:\s+ETA\s+(\S+))?"
)

# Stage boundaries on the overall progress bar. Downloading a multi-gigabyte
# episode and transcribing it are the long stages, so they own most of the bar;
# previously the whole download moved it from 5% to 11% and looked frozen.
STAGE_SPAN = {
    "resolve": (0.00, 0.02),
    "download": (0.02, 0.32),
    "transcribe": (0.32, 0.80),
    "select": (0.80, 0.83),
    "render": (0.83, 1.00),
}


def _stage_progress(stage: str, share: float) -> float:
    low, high = STAGE_SPAN[stage]
    return low + (high - low) * max(0.0, min(share, 1.0))


def normalize_source(raw: str) -> str:
    """Add the missing scheme to a bare YouTube link so it is treated as a URL."""
    source = raw.strip()
    if source.startswith(("http://", "https://")):
        return source
    if _URL_LIKE.match(source):
        return "https://" + source.lstrip("/")
    return source


def is_remote(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def is_collection(url: str) -> bool:
    """True when the URL names a channel or playlist rather than one video."""
    if "watch?v=" in url or "youtu.be/" in url:
        return False
    return any(marker in url for marker in _COLLECTION_MARKERS)


def resolve_episode(url: str, hook: ProgressHook) -> str:
    """Expand a channel/playlist URL to its most recent video URL."""
    hook("resolve", _stage_progress("resolve", 0.2), "Reading channel for the latest episode…")
    completed = subprocess.run(
        [
            binary("yt-dlp"), "--flat-playlist", "--playlist-end", "1",
            "--print", "%(id)s\t%(title)s", "--", url,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    line = completed.stdout.strip().splitlines()
    if not line:
        raise RuntimeError(f"No videos found at {url}")
    video_id, _, title = line[0].partition("\t")
    hook("resolve", _stage_progress("resolve", 1.0), f"Latest episode: {title[:70]}")
    return f"https://www.youtube.com/watch?v={video_id}"


def video_title(url: str) -> str:
    """Look up a video's title so jobs are listed by episode, not by id."""
    try:
        completed = subprocess.run(
            [binary("yt-dlp"), "--skip-download", "--print", "%(title)s", "--no-playlist", "--", url],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return completed.stdout.strip().splitlines()[0][:120]
    except Exception:
        return ""


def download(url: str, destination: Path, hook: ProgressHook) -> Path:
    """Fetch one video with yt-dlp, reporting real download progress."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            binary("yt-dlp"), "--newline", "--no-playlist",
            "-f", "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]",
            "--merge-output-format", "mp4",
            # YouTube throttles a single DASH connection hard (~0.6 MB/s on a
            # 2 GB episode); fetching fragments concurrently is several times
            # faster and is the difference between minutes and an hour.
            "--concurrent-fragments", "8",
            "--retries", "10", "--fragment-retries", "10",
            # yt-dlp shells out to FFmpeg to merge the streams, and it searches
            # PATH, which does not include Homebrew in every launch context.
            "--ffmpeg-location", binary("ffmpeg"),
            "-o", str(destination), "--", url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    tail: list[str] = []
    assert process.stdout is not None
    # yt-dlp fetches video and audio as two separate files, each reporting its
    # own 0-100% against its own size. Reported verbatim that looks like the
    # download restarting and the total size changing, so each stream is mapped
    # into its own slice of one monotonic bar.
    stream = 0
    for line in process.stdout:
        tail = (tail + [line.rstrip()])[-12:]
        if "Destination:" in line:
            stream += 1
        if "[Merger]" in line or "Merging formats" in line:
            hook("download", _stage_progress("download", 0.99), "Merging audio and video…")
            continue
        match = _PROGRESS.search(line)
        if not match:
            continue
        share = float(match.group(1)) / 100.0
        size, speed, eta = match.group(2), match.group(3), match.group(4)
        if stream <= 1:  # the video stream is the overwhelming majority of bytes
            overall, label = share * 0.92, "video"
        else:
            overall, label = 0.92 + share * 0.06, "audio"
        detail = f"{share * 100:.0f}%"
        if size:
            detail += f" of {size.replace(' ', '')}"
        if speed:
            detail += f" at {speed}"
        if eta:
            detail += f" · ETA {eta}"
        hook("download", _stage_progress("download", overall), f"Downloading {label} {detail}")
    if process.wait() != 0:
        raise RuntimeError("yt-dlp failed:\n" + "\n".join(tail))
    return destination


CHUNK_SECONDS = 480.0
# A `medium` model costs roughly 1.5 GB resident per process, so parallelism is
# capped by model size rather than by CPU count alone.
_MODEL_WORKER_CAP = {"tiny": 8, "base": 8, "small": 6, "medium": 3, "large-v3": 2}


def _extract_audio(video: Path, destination: Path) -> float:
    """Write mono 16 kHz PCM (what Whisper wants) and return its duration."""
    subprocess.run(
        [
            binary("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(video),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(destination),
        ],
        check=True,
        capture_output=True,
        timeout=3600,
    )
    completed = subprocess.run(
        [
            binary("ffprobe"), "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return float(completed.stdout.strip())


def _transcribe_chunk(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Worker entry point: transcribe one audio slice and rebase its timings."""
    from faster_whisper import WhisperModel

    model = WhisperModel(payload["model"], device="cpu", compute_type="int8")
    segments, _info = model.transcribe(
        payload["audio"],
        word_timestamps=True,
        vad_filter=True,
        beam_size=1,
        clip_timestamps=[payload["start"], payload["end"]],
    )
    words: list[dict[str, Any]] = []
    for segment in segments:
        for raw in segment.words or []:
            text = str(raw.word).strip()
            if not text:
                continue
            start = float(raw.start)
            words.append(
                {
                    "text": text,
                    "start": start,
                    "end": max(start, float(raw.end)),
                    "probability": float(getattr(raw, "probability", 1.0)),
                }
            )
    return words


def transcribe(
    video: Path, model_size: str, hook: ProgressHook, workers: int = 4
) -> list[Word]:
    """Transcribe by splitting the audio and running slices in parallel processes."""
    hook("transcribe", _stage_progress("transcribe", 0.02), "Extracting audio…")
    with tempfile.TemporaryDirectory(prefix="openclips-audio-") as temporary:
        audio = Path(temporary) / "audio.wav"
        duration = _extract_audio(video, audio)

        bounds: list[tuple[float, float]] = []
        cursor = 0.0
        while cursor < duration:
            bounds.append((cursor, min(cursor + CHUNK_SECONDS, duration)))
            cursor += CHUNK_SECONDS

        parallel = max(1, min(workers, _MODEL_WORKER_CAP.get(model_size, 4), len(bounds)))
        hook(
            "transcribe",
            _stage_progress("transcribe", 0.05),
            f"Transcribing {duration / 60:.0f} min with {model_size} × {parallel} workers…",
        )

        payloads = [
            {"audio": str(audio), "model": model_size, "start": start, "end": end}
            for start, end in bounds
        ]
        collected: list[list[dict[str, Any]]] = [[] for _ in payloads]
        done = 0
        with ProcessPoolExecutor(max_workers=parallel) as pool:
            futures = {
                pool.submit(_transcribe_chunk, payload): index
                for index, payload in enumerate(payloads)
            }
            for future in as_completed(futures):
                index = futures[future]
                collected[index] = future.result()
                done += 1
                hook(
                    "transcribe",
                    _stage_progress("transcribe", 0.05 + 0.95 * (done / len(payloads))),
                    f"Transcribed {done}/{len(payloads)} segments",
                )

    words = [Word(**word) for chunk in collected for word in chunk]
    words.sort(key=lambda word: (word.start, word.end))
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
            binary("ffmpeg"), "-nostdin", "-v", "error", "-y",
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
        source = normalize_source(source)
        state.source = source
        if is_remote(source):
            if is_collection(source):
                source = resolve_episode(source, report)
            state.title = video_title(source)
            video = download(source, MEDIA_DIR / "source" / f"{job_id}.mp4", report)
        else:
            video = Path(source).expanduser().resolve()
            if not video.is_file():
                raise FileNotFoundError(f"Source file not found: {video}")
        state.title = state.title or video.stem

        if transcript_path is not None:
            report("transcribe", _stage_progress("transcribe", 1.0), "Loading existing transcript…")
            words = load_words(transcript_path)
        else:
            words = transcribe(video, model_size, report, workers=workers)
        if not words:
            raise RuntimeError("Transcription produced no words")

        report("select", _stage_progress("select", 0.5), "Finding the strongest moments…")
        candidates = find_clips(build_sentences(words), max_clips=max_clips)
        if not candidates:
            raise RuntimeError("No clip-worthy segments found")

        report("render", _stage_progress("render", 0.0), f"Rendering {len(candidates)} clips in parallel…")
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
                    _stage_progress("render", done / len(payloads)),
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
