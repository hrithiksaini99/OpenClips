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
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from studio.captions import CaptionStyle
from studio.llm import ClipRanker
from studio.render import render_clip
from studio.select import ClipCandidate, find_clips
from studio.thumbnail import build as build_thumbnail
from studio.tools import binary
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
    thumbnail: str = ""   # small still, used as the poster in the results grid
    poster: str = ""      # 1280x720 thumbnail offered to YouTube
    video_id: str = ""    # set once posted; the clip file may then be gone


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


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON so a concurrent reader never sees a half-written file.

    Job state is rewritten several times a second while the UI polls it, and a
    plain write_text let a reader observe a truncated file: that surfaced as a
    500 from /api/jobs and killed the browser's polling loop. Writing to a
    sibling temp file and renaming makes the swap atomic. The publish queue is
    written the same way, for the same reason.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}"
    temporary.write_text(json.dumps(payload, indent=2))
    os.replace(temporary, path)


def write_state(state: JobState) -> None:
    atomic_write_json(job_dir(state.id) / "job.json", state.to_dict())


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
            *_yt_dlp_flags(),
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
            [
                binary("yt-dlp"), "--skip-download", "--print", "%(title)s",
                *_yt_dlp_flags(), "--no-playlist", "--", url,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return completed.stdout.strip().splitlines()[0][:120]
    except Exception:
        return ""


# yt-dlp enables only Deno by default, and YouTube extraction without a JS
# runtime is deprecated, so whatever is installed gets used.
_JS_RUNTIMES = ("deno", "node", "bun")


@lru_cache(maxsize=1)
def js_runtime() -> str:
    """The name of an installed JavaScript runtime, or "" if there is none."""
    for name in _JS_RUNTIMES:
        if shutil.which(name):
            return name
    return ""


def _auth_flags() -> list[str]:
    """Cookies for YouTube, when the machine has been told where to find them.

    YouTube increasingly answers an unauthenticated download with "sign in to
    confirm you're not a bot". Cookies are the documented fix, but they are the
    user's live session, so nothing is read unless one of these is set
    deliberately.
    """
    browser = os.environ.get("OPENCLIPS_COOKIES_FROM_BROWSER", "").strip()
    if browser:
        return ["--cookies-from-browser", browser]
    jar = os.environ.get("OPENCLIPS_COOKIES_FILE", "").strip()
    if jar:
        return ["--cookies", jar]
    return []


def _yt_dlp_flags() -> list[str]:
    """Flags every yt-dlp invocation should carry."""
    flags = _auth_flags()
    runtime = js_runtime()
    if runtime:
        flags += ["--js-runtimes", runtime]
    return flags


# Signatures worth translating: yt-dlp's own output is a dozen lines of warnings
# with the one actionable sentence buried in the middle.
_YT_DLP_HINTS = (
    (
        "HTTP Error 429",
        "YouTube is rate-limiting this machine, not rejecting the link. It usually "
        "lapses within a few minutes and the download is retried automatically; if "
        "it keeps happening, set OPENCLIPS_COOKIES_FROM_BROWSER.",
    ),
    (
        "not a bot",
        "YouTube challenged every player client this tried. It is usually "
        "intermittent, so the same link often works a few minutes later. If it "
        "persists, set OPENCLIPS_COOKIES_FROM_BROWSER to the browser you watch "
        "YouTube in (chrome, firefox, safari, edge, brave), or "
        "OPENCLIPS_COOKIES_FILE to an exported cookies.txt.",
    ),
    (
        "No supported JavaScript runtime",
        "yt-dlp needs a JavaScript runtime for YouTube. Install Deno or Node and it "
        "will be picked up automatically.",
    ),
    (
        "Video unavailable",
        "YouTube says this video is unavailable — private, deleted, or blocked in "
        "this region.",
    ),
)


def diagnose_download(output: str) -> str:
    """Turn yt-dlp's output into the one sentence that explains the failure."""
    for signature, explanation in _YT_DLP_HINTS:
        if signature in output:
            return explanation
    return ""


def is_rate_limited(output: str) -> bool:
    """True when YouTube is throttling this machine and a pause is the answer."""
    return "HTTP Error 429" in output


def is_bot_checked(output: str) -> bool:
    """True when YouTube challenged the client rather than the machine.

    Worth separating from a rate limit because the answer is different: a
    challenge is aimed at the player client that asked, and another client is
    usually waved through, so there is nothing to wait for.
    """
    return "not a bot" in output


def needs_patience(output: str) -> bool:
    """Neither of these is fixed by a newer yt-dlp, so no upgrade is spent."""
    return is_rate_limited(output) or is_bot_checked(output)


# A rate limit usually lapses in a couple of minutes, so it is waited out
# rather than failing a job that was minutes from starting.
RATE_LIMIT_ATTEMPTS = 3
RATE_LIMIT_WAIT = 60.0

# YouTube challenges the web client intermittently. This one is rarely
# challenged and offers the same formats — 1080p mp4 and 129k m4a — so falling
# back to it costs nothing in quality and saves asking anyone for cookies.
FALLBACK_PLAYER_CLIENT = "tv_embedded"

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

    upgraded = False
    client = ""
    for attempt in range(RATE_LIMIT_ATTEMPTS):
        try:
            return _run_yt_dlp(
                url, fmt=fmt, destination=destination, label=label, hook=hook,
                span=span, player_client=client,
            )
        except RuntimeError as error:
            last = attempt == RATE_LIMIT_ATTEMPTS - 1
            output = str(error)
            if is_bot_checked(output) and not client:
                # The challenge is aimed at the client that asked, so ask as a
                # different one. Nothing to wait for.
                client = FALLBACK_PLAYER_CLIENT
                if hook is not None:
                    hook("download", span[0], f"Retrying {label} as a different player…")
                continue
            if is_rate_limited(output):
                # A limit is usually a passing squall rather than a wall, so it
                # is waited out. Trying again straight away is what earned it.
                if last:
                    raise
                wait = RATE_LIMIT_WAIT * (attempt + 1)
                if hook is not None:
                    hook(
                        "download", span[0],
                        f"YouTube is rate-limiting; waiting {wait:.0f}s before retrying {label}…",
                    )
                time.sleep(wait)
                continue
            # Anything else is usually a YouTube-side change upstream has
            # already fixed, so it is worth one upgrade and one more go.
            if upgraded or last or needs_patience(output) or not _upgrade_yt_dlp():
                raise
            upgraded = True
            if hook is not None:
                hook("download", span[0], f"Updated yt-dlp, retrying {label}…")
    raise RuntimeError(f"Could not fetch the {label}")


def _run_yt_dlp(
    url: str,
    *,
    fmt: str,
    destination: Path,
    label: str,
    hook: ProgressHook | None,
    span: tuple[float, float],
    player_client: str = "",
) -> Path:
    process = subprocess.Popen(
        [
            binary("yt-dlp"), "--newline", "--no-playlist", "-f", fmt,
            *_yt_dlp_flags(),
            # YouTube throttles a single DASH connection hard (~0.6 MB/s on a
            # 2 GB stream), so fragments are fetched in parallel. Four rather
            # than eight: audio and video download at once, so this is doubled
            # in practice, and sixteen parallel requests is what earns a 429.
            "--concurrent-fragments", "4",
            "--retries", "10", "--fragment-retries", "10",
            # Back off inside the run rather than giving up on the first refusal.
            "--retry-sleep", "http:exp=1:120",
            "--extractor-retries", "3",
            "--sleep-requests", "1",
            "--ffmpeg-location", binary("ffmpeg"),
            *(["--extractor-args", f"youtube:player_client={player_client}"]
              if player_client else []),
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
        output = "\n".join(tail)
        hint = diagnose_download(output)
        headline = f"Could not fetch the {label}"
        raise RuntimeError(f"{headline}. {hint}\n\n{output}" if hint else f"{headline}:\n{output}")
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


def _split_wav(source: Path, into: Path, seconds: float) -> list[tuple[Path, float]]:
    """Cut a WAV into fixed-length pieces, returning each with its start offset.

    One ffmpeg pass with the segment muxer and a stream copy, so a 4.5-hour file
    is split in a second or two. The pieces are real short files: each is handed
    to Whisper whole, which is the point — see `transcribe`.
    """
    into.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            binary("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(source),
            "-f", "segment", "-segment_time", f"{seconds:.0f}", "-c", "copy",
            str(into / "part-%04d.wav"),
        ],
        check=True,
        capture_output=True,
        timeout=1800,
    )
    parts = sorted(into.glob("part-*.wav"))
    return [(part, index * seconds) for index, part in enumerate(parts)]


def transcribe(
    video: Path, model_size: str, hook: ProgressHook, workers: int = 4
) -> list[Word]:
    """Transcribe fixed-length audio pieces concurrently against one model.

    The audio is physically cut into pieces and each piece transcribed on its
    own. The obvious-looking alternative — one model, `clip_timestamps` to point
    each worker at a range of the whole file — does not work:
    WhisperModel.transcribe decodes the entire file and extracts features for
    all of it on every call, then only seeks past the start offset, so on a
    multi-hour episode four workers each grind through most of the file and the
    first result never lands. Real short files avoid all of that.

    Threads rather than processes for the model: a `small` worker measures
    ~845 MB before it transcribes a second, so a process pool exhausted memory
    and crashed the machine. CTranslate2 shares weights across its internal
    workers and releases the GIL during compute (measured: 838 MB for four
    workers against 3.4 GB).
    """
    from faster_whisper import WhisperModel

    hook("transcribe", _stage_progress("transcribe", 0.02), "Extracting audio…")
    with tempfile.TemporaryDirectory(prefix="openclips-audio-") as temporary:
        root = Path(temporary)
        audio = root / "audio.wav"
        duration = _extract_audio(video, audio)
        pieces = _split_wav(audio, root / "parts", CHUNK_SECONDS)
        audio.unlink(missing_ok=True)  # the pieces are all that is needed now

        parallel = max(1, min(workers, _MODEL_WORKER_CAP.get(model_size, 4), len(pieces)))
        parallel = _shared_model_workers(MODEL_FOOTPRINT_MB.get(model_size, 1200), parallel)

        hook("transcribe", _stage_progress("transcribe", 0.04), f"Loading {model_size} model…")
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

        # `model` is a default arg rather than a closure: it is deleted below to
        # free its memory, and a closure would make that ordering load-bearing.
        def run(index: int, model: Any = model) -> tuple[int, list[dict[str, Any]]]:
            part, offset = pieces[index]
            segments, _info = model.transcribe(
                str(part), word_timestamps=True, vad_filter=True, beam_size=1
            )
            words: list[dict[str, Any]] = []
            for segment in segments:
                for raw in segment.words or []:
                    text = str(raw.word).strip()
                    if not text:
                        continue
                    start = float(raw.start) + offset
                    words.append(
                        {
                            "text": text,
                            "start": start,
                            "end": max(start, float(raw.end) + offset),
                            "probability": float(getattr(raw, "probability", 1.0)),
                        }
                    )
            return index, words

        collected: list[list[dict[str, Any]]] = [[] for _ in pieces]
        done = 0
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            for index, chunk_words in pool.map(run, range(len(pieces))):
                collected[index] = chunk_words
                done += 1
                hook(
                    "transcribe",
                    _stage_progress("transcribe", 0.06 + 0.94 * (done / len(pieces))),
                    f"Transcribed {done}/{len(pieces)} segments",
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
    # Built from the original episode rather than the render: the render has
    # captions burned in, which read as clutter behind the thumbnail's own text.
    poster_path = Path(payload["destination"]).with_name(
        Path(payload["destination"]).stem + "-yt.jpg"
    )
    if not payload.get("make_poster"):
        return {"id": payload["id"], "thumbnail": thumbnail.name, "poster": ""}
    poster: Path | None = poster_path
    try:
        build_thumbnail(
            Path(payload["source"]),
            poster_path,
            payload.get("title", ""),
            at=payload["start"] + min(4.0, (payload["end"] - payload["start"]) / 3),
        )
    except Exception:
        poster = None  # a missing thumbnail must not lose the clip
    return {
        "id": payload["id"],
        "thumbnail": thumbnail.name,
        "poster": poster.name if poster else "",
    }


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

    hook(
        "select",
        _stage_progress("select", 0.5),
        f"Asking {ranker.model} to pick the best {limit}…",
    )
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
    delete_source: bool = False,
    make_thumbnails: bool = False,
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
        shortlist = find_clips(sentences, max_clips=min(max_clips * 3, 45))
        if not shortlist:
            raise RuntimeError("No clip-worthy segments found")
        candidates = rank_candidates(shortlist, max_clips, report, use_llm=use_llm)

        if pending_video is not None:
            report("render", _stage_progress("render", 0.0), "Waiting for the video download…")
            pending_video.result()
            downloads.shutdown(wait=True)
        report("render", _stage_progress("render", 0.0), f"Rendering {len(candidates)} clips…")
        payloads: list[dict[str, Any]] = []
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
                    "title": candidate.title,
                    "words": window,
                    "style": asdict(CaptionStyle()),
                    "face_track": face_track,
                    "make_poster": make_thumbnails,
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
        with ProcessPoolExecutor(max_workers=render_workers) as workers_pool:
            futures = [workers_pool.submit(_render_one, payload) for payload in payloads]
            for future in as_completed(futures):
                result = future.result()
                records[result["id"]].thumbnail = result["thumbnail"]
                records[result["id"]].poster = result["poster"]
                done += 1
                report(
                    "render",
                    _stage_progress("render", done / len(payloads)),
                    f"Rendered {done}/{len(payloads)} clips",
                )

        state.clips = [records[payload["id"]] for payload in payloads]
        state.status = "done"
        # Only once every clip is rendered, and only for a source we downloaded:
        # a local file belongs to the user, and a failed run needs its source to
        # be re-runnable.
        done_message = f"{len(state.clips)} clips ready"
        if delete_source and is_remote(state.source):
            freed = discard_source(state.source)
            if freed:
                done_message += f" · {freed / 1e9:.1f} GB freed"
        report("done", 1.0, done_message)
    except Exception as error:  # surface the failure in the UI rather than dying silently
        state.status = "error"
        state.error = f"{type(error).__name__}: {error}"
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            state.error += f" | {str(error.stderr)[-400:]}"
        report("error", state.progress, state.error)
    write_state(state)
    return state


def discard_source(url: str) -> int:
    """Delete the downloaded episode for `url`, returning the bytes freed.

    Only ever touches the cache under media/source, which OpenClips downloaded
    itself; a local file the user pointed at is not ours to remove.
    """
    base = MEDIA_DIR / "source" / cache_key(url)
    freed = 0
    for path in base.parent.glob(f"{base.name}.*"):
        try:
            size = path.stat().st_size
            path.unlink()
            freed += size
        except OSError:
            continue
    return freed


def mark_published(job_id: str, clip_id: str, video_id: str, *, drop_file: bool) -> int:
    """Record that a clip is on YouTube, optionally deleting the local MP4.

    The small poster JPEG is kept whatever happens: it costs 22 KB and it is
    what lets the results grid still show the clip once the video is gone.
    """
    state = read_state(job_id)
    if state is None:
        return 0
    freed = 0
    for clip in state.clips:
        if clip.id != clip_id:
            continue
        clip.video_id = video_id
        if drop_file:
            # The clip lives on YouTube now, so nothing local is worth keeping:
            # the render, its poster and its still all go, and the record goes
            # with them so the results grid stops listing a clip that is gone.
            for name in (clip.file, clip.thumbnail, clip.poster):
                if not name:
                    continue
                path = job_dir(job_id) / name
                try:
                    freed += path.stat().st_size
                    path.unlink()
                except OSError:
                    continue
            state.clips = [other for other in state.clips if other.id != clip_id]
        write_state(state)
        break
    return freed


def disk_usage() -> dict[str, int]:
    """Bytes held by downloaded sources and by rendered clips."""
    def total(root: Path) -> int:
        if not root.is_dir():
            return 0
        seen = 0
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    seen += path.stat().st_size
            except OSError:
                # yt-dlp churns through thousands of fragment files during a
                # download, so a path can vanish between rglob and stat. It is
                # a rounding error against a multi-gigabyte source either way.
                continue
        return seen

    return {"media": total(MEDIA_DIR), "clips": total(CLIPS_DIR)}


def recover_interrupted_jobs(active: set[str] | None = None) -> int:
    """Mark jobs whose worker is gone as interrupted. Returns how many.

    A job's state says "running" until its thread writes otherwise, and a
    process that was killed never got to. On a fresh start nothing is running,
    so any job still in a working state was interrupted. The downloaded audio is
    cached under the video id, so re-running the same link resumes from there
    rather than from zero.
    """
    running = active or set()
    fixed = 0
    for job in list_jobs():
        if job.id in running or job.status not in ("running", "queued"):
            continue
        job.status = "error"
        job.error = (
            "Interrupted — the server stopped mid-run. The download is cached, "
            "so starting the same link again picks up from there."
        )
        write_state(job)
        fixed += 1
    return fixed


def prune_empty_jobs(keep: set[str] | None = None) -> int:
    """Delete job folders that hold no clips. Returns how many went.

    A run that failed and a run whose clips have all been posted and reclaimed
    both leave an empty folder behind, and they pile up in the job list as rows
    that lead nowhere. Only `keep` is protected, so the caller decides what is
    still in flight rather than this guessing from a status that a killed
    process may have left behind.
    """
    protected = keep or set()
    empty = [job for job in list_jobs() if job.id not in protected and not job.clips]
    # list_jobs is newest first; the first empty job is the one still worth
    # looking at (a just-failed run), the rest are stale rows.
    removed = 0
    for job in empty[1:]:
        clear_job(job.id)
        removed += 1
    return removed


def clear_job(job_id: str) -> None:
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


def new_job_id() -> str:
    return uuid.uuid4().hex[:10]
