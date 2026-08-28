"""End-to-end pipeline: source in, postable vertical clips out.

Runs natively (no Docker, no database). State for a job lives in a single JSON
file beside its clips, which is enough for a local studio tool and keeps the
whole thing inspectable from Finder.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from studio.captions import CaptionStyle
from studio.llm import ClipRanker
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


AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio"
VIDEO_FORMAT = "bestvideo[ext=mp4][height<=1080]/best[ext=mp4]"


_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def cache_key(url: str) -> str:
    """A stable per-video name so the same episode is never fetched twice.

    Naming downloads after the job meant every run started from zero, and a
    restart threw away whatever had already arrived. Keyed by video id instead,
    a finished file is reused outright and a partial one is resumed by yt-dlp.
    """
    match = _VIDEO_ID.search(url)
    if match:
        return match.group(1)
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _upgrade_yt_dlp() -> bool:
    """Pull the newest yt-dlp.

    Nearly every download failure is YouTube changing something that upstream
    has already fixed, so an upgrade-and-retry recovers without a person
    noticing. Returns whether the upgrade command succeeded.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "yt-dlp"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        binary.cache_clear()  # the console script may have been replaced
        return completed.returncode == 0
    except Exception:
        return False


def _download_stream(
    url: str,
    *,
    fmt: str,
    destination: Path,
    label: str,
    hook: ProgressHook | None,
    span: tuple[float, float],
) -> Path:
    """Fetch one stream with yt-dlp, reporting progress into a slice of the bar.

    A completed file is reused as-is; a partial one is resumed, which yt-dlp
    does by default once the destination name is stable. A failure is retried
    once after upgrading yt-dlp, since YouTube-side changes are the usual cause
    and upstream has normally already shipped the fix.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size > 0:
        if hook is not None:
            hook("download", span[1], f"Reusing cached {label}")
        return destination
    try:
        return _run_yt_dlp(url, fmt=fmt, destination=destination, label=label, hook=hook, span=span)
    except RuntimeError:
        if not _upgrade_yt_dlp():
            raise
        if hook is not None:
            hook("download", span[0], f"Updated yt-dlp, retrying {label}…")
        return _run_yt_dlp(url, fmt=fmt, destination=destination, label=label, hook=hook, span=span)


def _run_yt_dlp(
    url: str,
    *,
    fmt: str,
    destination: Path,
    label: str,
    hook: ProgressHook | None,
    span: tuple[float, float],
) -> Path:
    process = subprocess.Popen(
        [
            binary("yt-dlp"), "--newline", "--no-playlist", "-f", fmt,
            # YouTube throttles a single DASH connection hard (~0.6 MB/s on a
            # 2 GB stream); concurrent fragments are several times faster.
            "--concurrent-fragments", "8",
            "--retries", "10", "--fragment-retries", "10",
            "--ffmpeg-location", binary("ffmpeg"),
            "-o", str(destination), "--", url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    tail: list[str] = []
    low, high = span
    assert process.stdout is not None
    for line in process.stdout:
        tail = (tail + [line.rstrip()])[-12:]
        match = _PROGRESS.search(line)
        if not match or hook is None:
            continue
        share = float(match.group(1)) / 100.0
        size, speed, eta = match.group(2), match.group(3), match.group(4)
        detail = f"{share * 100:.0f}%"
        if size:
            detail += f" of {size.replace(' ', '')}"
        if speed:
            detail += f" at {speed}"
        if eta:
            detail += f" \u00b7 ETA {eta}"
        hook("download", low + (high - low) * share, f"Downloading {label} {detail}")
    if process.wait() != 0:
        raise RuntimeError(f"yt-dlp failed fetching {label}:\n" + "\n".join(tail))
    return destination


@dataclass(frozen=True)
class Sources:
    """Audio and video kept as separate files.

    yt-dlp fetches them separately anyway and then spends minutes merging a
    multi-gigabyte MP4. Nothing downstream needs that merge: transcription reads
    the audio, and rendering takes both as two inputs.
    """

    video: Path
    audio: Path


def download_sources(
    url: str, base: Path, hook: ProgressHook, workers: int = 2
) -> Sources:
    """Fetch audio and video concurrently, returning as soon as both are on disk.

    Audio is small (~140 MB against ~2.7 GB) and arrives in well under a minute,
    so the caller can begin transcribing while the video is still downloading.
    """
    audio_path = base.with_suffix(".m4a")
    video_path = base.with_suffix(".mp4")
    del workers

    with ThreadPoolExecutor(max_workers=2) as pool:
        audio_future = pool.submit(
            _download_stream, url, fmt=AUDIO_FORMAT, destination=audio_path,
            label="audio", hook=hook, span=(_stage_progress("download", 0.0),
                                            _stage_progress("download", 0.15)),
        )
        video_future = pool.submit(
            _download_stream, url, fmt=VIDEO_FORMAT, destination=video_path,
            label="video", hook=hook, span=(_stage_progress("download", 0.15),
                                            _stage_progress("download", 1.0)),
        )
        return Sources(video=video_future.result(), audio=audio_future.result())


CHUNK_SECONDS = 480.0
# A `medium` model costs roughly 1.5 GB resident per process, so parallelism is
# capped by model size rather than by CPU count alone.
_MODEL_WORKER_CAP = {"tiny": 8, "base": 8, "small": 6, "medium": 4, "large-v3": 3}
# Measured resident cost of one loaded model; used to size concurrency to RAM.
MODEL_FOOTPRINT_MB = {"tiny": 250, "base": 400, "small": 900, "medium": 2000, "large-v3": 3400}
# One 1080x1920 encode plus its caption rendering.
RENDER_FOOTPRINT_MB = 700
# Activation + audio buffers a shared-model worker adds on top of the weights.
PER_WORKER_DELTA_MB = 400


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


def available_memory_mb() -> float:
    """Memory we may actually use right now, in MB.

    psutil is used rather than os.sysconf because the deployment target is
    Windows, where sysconf does not exist.
    """
    try:
        import psutil

        return psutil.virtual_memory().available / 1048576
    except Exception:
        return 4096.0  # conservative guess rather than an unbounded pool


def _memory_budget_workers(per_worker_mb: float, requested: int, floor: int = 1) -> int:
    """Cap concurrency to what free RAM can actually hold.

    A pool sized only by CPU count is what took the machine down: six
    transcription workers at ~1.5 GB each asked for ~9 GB on a box that had far
    less free. Two thirds of currently-available memory is offered to the pool
    and the rest is left for the OS, the browser and page cache.
    """
    affordable = int((available_memory_mb() * 0.66) // max(per_worker_mb, 1))
    return max(floor, min(requested, affordable))


def _shared_model_workers(model_mb: float, requested: int) -> int:
    """Size a shared-model pool: the weights are paid for once, not per worker.

    Measured: one `small` model is ~845 MB and stays ~838 MB with four internal
    workers, so only the per-worker activation and audio buffers scale. Budget
    the weights once and a modest delta per worker.
    """
    budget = available_memory_mb() * 0.66
    if budget <= model_mb:
        return 1  # too tight to parallelise, but still run
    affordable = int((budget - model_mb) // PER_WORKER_DELTA_MB)
    return max(1, min(requested, affordable))


def transcribe(
    video: Path, model_size: str, hook: ProgressHook, workers: int = 4
) -> list[Word]:
    """Transcribe audio slices concurrently against a single shared model.

    Earlier this used a process pool, which gave every worker its own copy of
    the model: a `small` worker measures ~845 MB before it transcribes a single
    second, so six of them exhausted memory and hard-crashed the machine.
    CTranslate2 shares model weights across its internal workers and releases
    the GIL during compute, so threads give the same parallelism for roughly the
    memory of one model (measured: 838 MB for four workers, versus 3.4 GB).
    """
    from faster_whisper import WhisperModel

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
        parallel = _shared_model_workers(MODEL_FOOTPRINT_MB.get(model_size, 1200), parallel)

        hook(
            "transcribe",
            _stage_progress("transcribe", 0.04),
            f"Loading {model_size} model…",
        )
        model = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
            cpu_threads=max(1, min(4, (os.cpu_count() or 4) // parallel)),
            num_workers=parallel,
        )
        hook(
            "transcribe",
            _stage_progress("transcribe", 0.06),
            f"Transcribing {duration / 60:.0f} min with {model_size} x {parallel}…",
        )

        def run(index: int) -> tuple[int, list[dict[str, Any]]]:
            begin, finish = bounds[index]
            segments, _info = model.transcribe(
                str(audio),
                word_timestamps=True,
                vad_filter=True,
                beam_size=1,
                clip_timestamps=[begin, finish],
            )
            words: list[dict[str, Any]] = []
            for segment in segments:
                for raw in segment.words or []:
                    text = str(raw.word).strip()
                    if not text:
                        continue
                    begin_at = float(raw.start)
                    words.append(
                        {
                            "text": text,
                            "start": begin_at,
                            "end": max(begin_at, float(raw.end)),
                            "probability": float(getattr(raw, "probability", 1.0)),
                        }
                    )
            return index, words

        collected: list[list[dict[str, Any]]] = [[] for _ in bounds]
        done = 0
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            for index, words in pool.map(run, range(len(bounds))):
                collected[index] = words
                done += 1
                hook(
                    "transcribe",
                    _stage_progress("transcribe", 0.06 + 0.94 * (done / len(bounds))),
                    f"Transcribed {done}/{len(bounds)} segments",
                )
        del model

    words = [Word(**word) for chunk in collected for word in chunk]
    words.sort(key=lambda word: (word.start, word.end))
    return words


def _render_one(payload: dict[str, Any]) -> dict[str, Any]:
    """Worker entry point: render a single clip in its own process."""
    words = [Word(**word) for word in payload["words"]]
    render_clip(
        source=Path(payload["source"]),
        audio=Path(payload["audio"]) if payload.get("audio") else None,
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


def rank_candidates(
    pool: list[ClipCandidate],
    limit: int,
    hook: ProgressHook,
    *,
    use_llm: bool = True,
) -> list[ClipCandidate]:
    """Let a local model choose the final clips, falling back to the heuristics.

    The heuristics find spans that are well formed and energetic; they cannot
    judge whether a moment is worth watching. The model reads the shortlist and
    scores it, and its title replaces the first sentence when it gives one.
    Blending keeps a little of the heuristic signal so one odd model answer
    cannot promote a rambling span.
    """
    if len(pool) <= limit:
        return pool
    if not use_llm:
        return sorted(pool, key=lambda candidate: candidate.start)[:limit]

    ranker = ClipRanker()
    if not ranker.available():
        hook("select", _stage_progress("select", 0.9), "Ranking locally (no LLM available)…")
        return sorted(pool[:limit], key=lambda candidate: candidate.start)

    hook("select", _stage_progress("select", 0.5), f"Asking {ranker.model} to pick the best {limit}…")
    rankings = {rank.index: rank for rank in ranker.rank([c.text for c in pool])}
    if not rankings:
        return sorted(pool[:limit], key=lambda candidate: candidate.start)

    heuristic_top = max((c.score for c in pool), default=1.0) or 1.0
    scored: list[tuple[float, ClipCandidate]] = []
    for index, candidate in enumerate(pool):
        rank = rankings.get(index)
        if rank is None:
            continue  # unscored by the model: leave it out rather than guess
        blended = rank.score * 0.8 + (candidate.score / heuristic_top) * 100 * 0.2
        title = rank.title if len(rank.title) > 8 else candidate.title
        scored.append((blended, replace(candidate, title=title, score=round(blended, 2))))

    if not scored:
        return sorted(pool[:limit], key=lambda candidate: candidate.start)
    scored.sort(key=lambda pair: -pair[0])
    chosen = [candidate for _score, candidate in scored[:limit]]
    hook("select", _stage_progress("select", 1.0), f"{ranker.model} picked {len(chosen)} clips")
    return sorted(chosen, key=lambda candidate: candidate.start)


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
    use_llm: bool = True,
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
        audio: Path | None = None
        pending_video: Any = None
        if is_remote(source):
            if is_collection(source):
                source = resolve_episode(source, report)
            state.title = video_title(source)
            # Keyed by video rather than by job, so a re-run of the same
            # episode reuses what is already on disk instead of refetching it.
            base = MEDIA_DIR / "source" / cache_key(source)
            base.parent.mkdir(parents=True, exist_ok=True)
            # Audio is ~140 MB and lands in under a minute; the video is ~2.7 GB.
            # Start both, then transcribe from the audio while the video streams
            # in behind us, so the two slowest stages overlap.
            downloads = ThreadPoolExecutor(max_workers=2)
            audio_job = downloads.submit(
                _download_stream, source, fmt=AUDIO_FORMAT,
                destination=base.with_suffix(".m4a"), label="audio", hook=report,
                span=(_stage_progress("download", 0.0), _stage_progress("download", 1.0)),
            )
            pending_video = downloads.submit(
                _download_stream, source, fmt=VIDEO_FORMAT,
                destination=base.with_suffix(".mp4"), label="video", hook=None,
                span=(0.0, 0.0),
            )
            audio = audio_job.result()
            video = base.with_suffix(".mp4")
        else:
            video = Path(source).expanduser().resolve()
            if not video.is_file():
                raise FileNotFoundError(f"Source file not found: {video}")
        state.title = state.title or video.stem

        if transcript_path is not None:
            report("transcribe", _stage_progress("transcribe", 1.0), "Loading existing transcript…")
            words = load_words(transcript_path)
        else:
            words = transcribe(audio or video, model_size, report, workers=workers)
        if not words:
            raise RuntimeError("Transcription produced no words")

        report("select", _stage_progress("select", 0.3), "Finding the strongest moments…")
        sentences = build_sentences(words)
        # Shortlist wider than needed so the model has something to choose from.
        pool = find_clips(sentences, max_clips=min(max_clips * 3, 45))
        if not pool:
            raise RuntimeError("No clip-worthy segments found")
        candidates = rank_candidates(pool, max_clips, report, use_llm=use_llm)

        if pending_video is not None:
            report("render", _stage_progress("render", 0.0), "Waiting for the video download…")
            pending_video.result()
            downloads.shutdown(wait=True)
        report("render", _stage_progress("render", 0.0), f"Rendering {len(candidates)} clips…")
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
                    "audio": str(audio) if audio else None,
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
        render_workers = _memory_budget_workers(RENDER_FOOTPRINT_MB, workers)
        report(
            "render",
            _stage_progress("render", 0.0),
            f"Rendering {len(candidates)} clips ({render_workers} at a time)…",
        )
        with ProcessPoolExecutor(max_workers=render_workers) as pool:
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
