"""Clip candidate value objects and selection bounds for boundary refinement."""

from dataclasses import dataclass

MIN_CLIPS = 3
MAX_CLIPS = 30
DEFAULT_MAX_CLIPS = 10
MIN_CLIP_SECONDS = 20.0
MAX_CLIP_SECONDS = 90.0


@dataclass(frozen=True)
class SelectionBounds:
    """Configurable clip-count and duration limits for one selection pass."""

    max_clips: int = DEFAULT_MAX_CLIPS
    min_duration_seconds: float = MIN_CLIP_SECONDS
    max_duration_seconds: float = MAX_CLIP_SECONDS

    def __post_init__(self) -> None:
        if not MIN_CLIPS <= self.max_clips <= MAX_CLIPS:
            msg = f"max_clips must be between {MIN_CLIPS} and {MAX_CLIPS}, got {self.max_clips}"
            raise ValueError(msg)
        if self.min_duration_seconds < 1.0:
            msg = f"min_duration_seconds must be at least 1.0, got {self.min_duration_seconds}"
            raise ValueError(msg)
        if self.max_duration_seconds < self.min_duration_seconds:
            msg = (
                f"max_duration_seconds {self.max_duration_seconds} is below "
                f"min_duration_seconds {self.min_duration_seconds}"
            )
            raise ValueError(msg)


@dataclass(frozen=True)
class ClipCandidate:
    """A proposed clip span with a deterministic score and derived title."""

    start: float
    end: float
    title: str
    score: float
    segment_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.end <= self.start:
            msg = f"Candidate span [{self.start}, {self.end}] is empty or inverted"
            raise ValueError(msg)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def overlaps(self, other: "ClipCandidate", tolerance: float = 0.0) -> bool:
        return self.start < other.end - tolerance and other.start < self.end - tolerance
