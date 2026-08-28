"""The publishing queue and the scheduler that drains it.

A clip becomes a queue entry with a written post attached; a background thread
posts one entry at each configured time of day. Everything lives in a single
JSON file next to the jobs, so the queue is inspectable from Finder in the same
way job state is.

The scheduler is deliberately conservative about firing. It posts at most one
clip per tick, claims a slot before uploading rather than after, and refuses to
catch up on slots older than a couple of hours — an unattended schedule that
dumps a day's backlog the moment the laptop wakes is worse than one that misses
a slot.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as clock, timedelta
from pathlib import Path

from studio import pipeline, youtube
from studio.metadata import PostWriter

STATE_FILE = pipeline.CLIPS_DIR / "publish.json"
TICK_SECONDS = 30.0
# A slot missed while the machine was asleep is still worth posting, but only
# for a while.
CATCHUP_GRACE = timedelta(hours=2)
# videos.insert bills to its own quota bucket: 1 unit a call, 100 calls a day.
DAILY_CAP = 100
MAX_ATTEMPTS = 3

_lock = threading.RLock()


@dataclass
class Entry:
    id: str
    job_id: str
    clip_id: str
    file: str
    title: str
    description: str
    hashtags: list[str] = field(default_factory=list)
    privacy: str = "private"
    status: str = "pending"  # pending | uploading | posted | failed
    video_id: str = ""
    error: str = ""
    attempts: int = 0
    thumbnail: str = ""
    duration: float = 0.0
    created_at: float = field(default_factory=time.time)
    posted_at: float = 0.0
    freed: int = 0  # bytes reclaimed by deleting the clip after posting


@dataclass
class Schedule:
    # Arming the schedule is the user's action, in the UI. It never starts on.
    enabled: bool = False
    slots: list[str] = field(default_factory=lambda: ["09:00", "15:00", "20:00"])
    # Uploads from an API project that has not passed Google's audit are locked
    # private whatever this says, so private is both the honest default and the
    # one setting to change once the audit clears.
    privacy: str = "private"
    category_id: str = "22"  # People & Blogs
    made_for_kids: bool = False
    auto_enqueue: bool = True


@dataclass
class Storage:
    """What to throw away once it has done its job.

    Both are permanent deletions, so both are toggles. Dropping sources undoes
    the download cache: re-running an episode then re-fetches the whole file.
    """

    delete_source_after_render: bool = True
    delete_clip_after_post: bool = True
    # Off by default: a channel that has not been phone-verified cannot set
    # custom thumbnails, so generating them would be work for a file YouTube
    # will refuse. Turn it on once youtube.com/verify is done.
    custom_thumbnails: bool = False


@dataclass
class Board:
    schedule: Schedule = field(default_factory=Schedule)
    storage: Storage = field(default_factory=Storage)
    queue: list[Entry] = field(default_factory=list)
    claimed: list[str] = field(default_factory=list)  # slot keys already fired
    last_error: str = ""


def _only_known(payload: dict, cls: type) -> dict:
    """Drop keys the dataclass does not have.

    Job state uses a strict `**payload`, which means an older file breaks the
    moment a field is added. The queue outlives releases, so it forgives both
    directions instead.
    """
    return {key: value for key, value in payload.items() if key in cls.__dataclass_fields__}


def load() -> Board:
    if not STATE_FILE.is_file():
        return Board()
    try:
        payload = json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return Board()  # a writer was mid-swap; the next read sees it
    return Board(
        schedule=Schedule(**_only_known(payload.get("schedule", {}), Schedule)),
        storage=Storage(**_only_known(payload.get("storage", {}), Storage)),
        queue=[Entry(**_only_known(item, Entry)) for item in payload.get("queue", [])],
        claimed=list(payload.get("claimed", []))[-64:],
        last_error=payload.get("last_error", ""),
    )


def save(board: Board) -> None:
    pipeline.atomic_write_json(
        STATE_FILE,
        {
            "schedule": asdict(board.schedule),
            "storage": asdict(board.storage),
            "queue": [asdict(entry) for entry in board.queue],
            "claimed": board.claimed[-64:],
            "last_error": board.last_error,
        },
    )


def _slot_times(slots: list[str], day: date) -> list[datetime]:
    """Parse "HH:MM" strings into datetimes on `day`, ignoring malformed ones."""
    moments: list[datetime] = []
    for slot in slots:
        hour, _, minute = str(slot).partition(":")
        try:
            moments.append(datetime.combine(day, clock(int(hour), int(minute))))
        except (ValueError, TypeError):
            continue
    return sorted(moments)


def due_slot(schedule: Schedule, claimed: list[str], now: datetime) -> str | None:
    """The slot key that should fire right now, or None.

    Yesterday is considered as well, so a late-evening slot still posts when the
    clock crosses midnight before the tick lands.
    """
    for day in (now.date() - timedelta(days=1), now.date()):
        for moment in _slot_times(schedule.slots, day):
            key = moment.strftime("%Y-%m-%dT%H:%M")
            if key in claimed:
                continue
            if moment <= now <= moment + CATCHUP_GRACE:
                return key
    return None


def posted_today(queue: list[Entry], now: datetime) -> int:
    midnight = datetime.combine(now.date(), clock(0, 0)).timestamp()
    return sum(1 for entry in queue if entry.status == "posted" and entry.posted_at >= midnight)


def _daily_limit(schedule: Schedule) -> int:
    """How many clips go up in a day: one per chosen time.

    This used to be a separate number, which meant the grid could say 24 times
    and the limit could say 10, with nothing on screen explaining which won.
    The times are now the whole answer.
    """
    return max(1, min(len(schedule.slots), DAILY_CAP))


def upcoming(board: Board, now: datetime | None = None) -> dict[str, str]:
    """Map each waiting entry to the time it is expected to post.

    Only a projection for the UI: the scheduler decides for real at each tick.
    """
    now = now or datetime.now()
    waiting = [entry for entry in board.queue if entry.status in ("pending", "uploading")]
    if not board.schedule.enabled or not waiting:
        return {}
    limit = _daily_limit(board.schedule)
    times: dict[str, str] = {}
    day = now.date()
    index = 0
    while index < len(waiting) and (day - now.date()).days < 60:
        used = posted_today(board.queue, now) if day == now.date() else 0
        for moment in _slot_times(board.schedule.slots, day):
            if index >= len(waiting) or used >= limit:
                break
            if moment <= now:
                continue
            times[waiting[index].id] = moment.isoformat(timespec="minutes")
            index += 1
            used += 1
        day += timedelta(days=1)
    return times


def find(entry_id: str) -> Entry | None:
    return next((entry for entry in load().queue if entry.id == entry_id), None)


def enqueue(job_id: str, clip_ids: list[str] | None = None, *, writer: PostWriter | None = None) -> list[Entry]:
    """Write posts for a job's clips and add them to the queue.

    Metadata is generated here rather than during rendering, so a post can be
    rewritten later without re-rendering the clip it belongs to.
    """
    state = pipeline.read_state(job_id)
    if state is None:
        raise LookupError(f"No such job: {job_id}")
    board = load()
    already = {(entry.job_id, entry.clip_id) for entry in board.queue}
    wanted = [
        clip
        for clip in state.clips
        if (clip_ids is None or clip.id in clip_ids) and (job_id, clip.id) not in already
    ]
    if not wanted:
        return []

    writer = writer or PostWriter()
    source_url = state.source if state.source.startswith(("http://", "https://")) else ""
    privacy = board.schedule.privacy
    added: list[Entry] = []
    for clip in wanted:
        post = writer.write(
            text=clip.text,
            fallback_title=clip.title,
            source_title=state.title,
            source_url=source_url,
        )
        added.append(
            Entry(
                id=uuid.uuid4().hex[:10],
                job_id=job_id,
                clip_id=clip.id,
                file=clip.file,
                title=post.title,
                description=post.description,
                hashtags=list(post.hashtags),
                privacy=privacy,
                thumbnail=clip.thumbnail,
                duration=clip.duration,
            )
        )
    # Re-read inside the lock: writing the posts above took a while, and the
    # queue may have moved on since.
    with _lock:
        board = load()
        board.queue.extend(added)
        save(board)
    return added


def edit(entry_id: str, changes: dict) -> Entry | None:
    """Change an entry's post. A posted entry is left alone."""
    allowed = {"title", "description", "hashtags", "privacy"}
    with _lock:
        board = load()
        for entry in board.queue:
            if entry.id != entry_id or entry.status == "posted":
                continue
            for key, value in changes.items():
                if key in allowed and value is not None:
                    setattr(entry, key, value)
            save(board)
            return entry
    return None


def remove(entry_id: str) -> bool:
    with _lock:
        board = load()
        remaining = [entry for entry in board.queue if entry.id != entry_id]
        if len(remaining) == len(board.queue):
            return False
        board.queue = remaining
        save(board)
        return True


def retry(entry_id: str) -> Entry | None:
    """Put a failed entry back in line with a clean slate."""
    with _lock:
        board = load()
        for entry in board.queue:
            if entry.id == entry_id and entry.status == "failed":
                entry.status = "pending"
                entry.attempts = 0
                entry.error = ""
                save(board)
                return entry
    return None


def configure(changes: dict) -> Schedule:
    with _lock:
        board = load()
        for key, value in changes.items():
            if value is None:
                continue
            if key in Schedule.__dataclass_fields__:
                setattr(board.schedule, key, value)
            elif key in Storage.__dataclass_fields__:
                setattr(board.storage, key, value)
        save(board)
        return board.schedule


def claim_now(entry_id: str) -> Entry:
    """Reserve one entry to be posted immediately, outside the schedule.

    Marking it "uploading" inside the lock is what stops a tick that lands
    mid-click from picking the same entry up and posting it twice.
    """
    if not youtube.connected():
        raise youtube.NotConnected("Connect a YouTube account first")
    with _lock:
        board = load()
        if posted_today(board.queue, datetime.now()) >= DAILY_CAP:
            raise RuntimeError(f"YouTube's cap of {DAILY_CAP} uploads a day is used up")
        for entry in board.queue:
            if entry.id != entry_id:
                continue
            if entry.status in ("uploading", "posted"):
                raise RuntimeError(f"That clip is already {entry.status}")
            # A manual publish is an explicit decision, so a parked entry gets a
            # clean slate rather than being refused for its earlier failures.
            entry.status = "uploading"
            entry.error = ""
            entry.attempts = 0
            save(board)
            return entry
    raise LookupError("Entry not found")


def claim_batch(limit: int | None = None) -> list[str]:
    """Reserve every waiting entry for an immediate publish.

    Claimed in one pass under the lock so a scheduler tick landing mid-batch
    cannot pick up an entry this batch is about to post. The daily cap is
    YouTube's real one rather than the schedule's pacing limit, which is a
    preference and not a rule.
    """
    if not youtube.connected():
        raise youtube.NotConnected("Connect a YouTube account first")
    with _lock:
        board = load()
        room = DAILY_CAP - posted_today(board.queue, datetime.now())
        if room <= 0:
            raise RuntimeError(f"YouTube's cap of {DAILY_CAP} uploads a day is used up")
        if limit is not None:
            room = min(room, max(0, limit))
        claimed: list[str] = []
        for entry in board.queue:
            if len(claimed) >= room:
                break
            if entry.status not in ("pending", "failed"):
                continue
            entry.status = "uploading"
            entry.error = ""
            entry.attempts = 0
            claimed.append(entry.id)
        if not claimed:
            raise RuntimeError("Nothing in the queue is waiting to be posted")
        save(board)
        return claimed


def _finish(entry_id: str, *, video_id: str = "", error: str = "", note: str = "") -> None:
    with _lock:
        board = load()
        for entry in board.queue:
            if entry.id != entry_id:
                continue
            if video_id:
                entry.status = "posted"
                entry.video_id = video_id
                entry.posted_at = time.time()
                entry.error = note
                # The clip is on YouTube now, so the local copy is redundant.
                # Recording the id on the job first means the results grid can
                # link to the video instead of a file that is no longer there.
                entry.freed = pipeline.mark_published(
                    entry.job_id, entry.clip_id, video_id,
                    drop_file=board.storage.delete_clip_after_post,
                )
            else:
                entry.attempts += 1
                entry.error = error
                # Back to pending so the next slot retries: one bad upload is
                # usually the network. Only a run of them parks the entry.
                entry.status = "failed" if entry.attempts >= MAX_ATTEMPTS else "pending"
            break
        board.last_error = error
        save(board)


def note_error(message: str) -> None:
    """Record a failure that happened outside a tick, so the UI can show it."""
    with _lock:
        board = load()
        board.last_error = message
        save(board)


class Scheduler:
    """Posts one queued clip per configured slot, from its own thread."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="openclips-scheduler"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception as error:  # a bad tick must not kill the thread
                with _lock:
                    board = load()
                    board.last_error = f"{type(error).__name__}: {error}"
                    save(board)

    def tick(self, now: datetime | None = None) -> str | None:
        """Fire at most one slot. Returns the id of the entry posted, if any."""
        entry_id = self._claim(now or datetime.now())
        if entry_id is None:
            return None
        self._post(entry_id)
        return entry_id

    def _claim(self, now: datetime) -> str | None:
        """Reserve one due slot and one waiting entry, atomically.

        The slot is claimed before the upload rather than after, so a crash
        halfway through cannot let the next tick post a second clip into it.
        """
        with _lock:
            board = load()
            if not board.schedule.enabled or not youtube.connected():
                return None
            if posted_today(board.queue, now) >= _daily_limit(board.schedule):
                return None
            slot = due_slot(board.schedule, board.claimed, now)
            if slot is None:
                return None
            entry = next((item for item in board.queue if item.status == "pending"), None)
            if entry is None:
                return None
            entry.status = "uploading"
            entry.error = ""
            board.claimed.append(slot)
            save(board)
            return entry.id

    def post(self, entry_id: str) -> None:
        """Upload one already-claimed entry. Used by the tick and by Publish now."""
        self._post(entry_id)

    def post_batch(self, entry_ids: list[str]) -> None:
        """Upload a claimed batch, one at a time.

        Sequential on purpose: parallel uploads to one channel invite throttling,
        and a clip that fails should not take the rest of the batch with it.
        """
        for entry_id in entry_ids:
            try:
                self._post(entry_id)
            except Exception as error:
                _finish(entry_id, error=f"{type(error).__name__}: {error}")

    def _post(self, entry_id: str) -> None:
        entry = find(entry_id)
        if entry is None:
            return
        board = load()
        schedule, schedule_storage = board.schedule, board.storage
        path = pipeline.job_dir(entry.job_id) / entry.file
        try:
            if not path.is_file():
                raise FileNotFoundError(f"Clip file is missing: {path}")
            video_id = youtube.upload(
                path,
                title=entry.title,
                description=entry.description,
                tags=[tag.lstrip("#") for tag in entry.hashtags],
                privacy=entry.privacy or schedule.privacy,
                category_id=schedule.category_id,
                made_for_kids=schedule.made_for_kids,
            )
        except Exception as error:
            _finish(entry_id, error=f"{type(error).__name__}: {error}")
            return
        # Cosmetic, and commonly refused, so it happens after the upload is
        # already banked and never turns a posted clip into a failed one.
        note = ""
        if schedule_storage.custom_thumbnails:
            clip = pipeline.read_state(entry.job_id)
            record = next((c for c in clip.clips if c.id == entry.clip_id), None) if clip else None
            if record and record.poster:
                poster = pipeline.job_dir(entry.job_id) / record.poster
                if poster.is_file():
                    try:
                        youtube.set_thumbnail(video_id, poster)
                    except Exception as error:
                        note = f"Posted, but the thumbnail was refused: {error}"
        _finish(entry_id, video_id=video_id, note=note)


scheduler = Scheduler()
