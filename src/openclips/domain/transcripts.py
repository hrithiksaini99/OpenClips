"""Normalized transcript data shared by transcription, selection, and rendering."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptWord:
    """A single word with normalized second-based timing."""

    text: str
    start: float
    end: float
    probability: float

    def __post_init__(self) -> None:
        if self.end < self.start:
            msg = f"Word end {self.end} precedes start {self.start}"
            raise ValueError(msg)
        if not 0.0 <= self.probability <= 1.0:
            msg = f"Word probability {self.probability} is outside [0, 1]"
            raise ValueError(msg)


@dataclass(frozen=True)
class TranscriptSegment:
    """A contiguous span of words with normalized second-based timing."""

    start: float
    end: float
    text: str
    words: tuple[TranscriptWord, ...]

    def __post_init__(self) -> None:
        if self.end < self.start:
            msg = f"Segment end {self.end} precedes start {self.start}"
            raise ValueError(msg)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class TranscriptDocument:
    """A complete transcript for one source asset."""

    language: str
    duration: float
    segments: tuple[TranscriptSegment, ...]

    def __post_init__(self) -> None:
        if self.duration < 0.0:
            msg = f"Transcript duration {self.duration} is negative"
            raise ValueError(msg)

    @property
    def word_count(self) -> int:
        return sum(len(segment.words) for segment in self.segments)

    @property
    def full_text(self) -> str:
        return " ".join(segment.text.strip() for segment in self.segments if segment.text.strip())
