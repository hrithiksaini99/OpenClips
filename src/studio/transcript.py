"""Transcript loading and sentence segmentation.

The clip quality of the whole pipeline depends on this module: candidate clips
are built from *sentences*, never from arbitrary time windows. That is what
keeps a clip from starting mid-thought ("And Paul Icy which is just the name").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# A sentence ends on terminal punctuation, but Whisper also emits long runs with
# no punctuation at all; a large pause is treated as a soft boundary.
_TERMINAL = re.compile(r"[.!?]['\")\]]*$")
_SOFT_PAUSE_SECONDS = 0.65
_MAX_SENTENCE_SECONDS = 18.0


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    probability: float = 1.0


@dataclass
class Sentence:
    words: list[Word] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words).strip()

    @property
    def is_terminated(self) -> bool:
        return bool(self.words) and _TERMINAL.search(self.words[-1].text) is not None


def load_words(transcript_path: Path) -> list[Word]:
    """Read a Whisper-style transcript document into a flat word timeline."""
    payload = json.loads(transcript_path.read_text())
    words: list[Word] = []
    for segment in payload.get("segments", []):
        for raw in segment.get("words", []):
            text = str(raw.get("text", "")).strip()
            if not text:
                continue
            start = float(raw["start"])
            end = max(start, float(raw["end"]))
            words.append(
                Word(
                    text=text,
                    start=start,
                    end=end,
                    probability=float(raw.get("probability", 1.0)),
                )
            )
    words.sort(key=lambda word: (word.start, word.end))
    return words


def build_sentences(words: list[Word]) -> list[Sentence]:
    """Group a word timeline into sentences on punctuation or long pauses."""
    sentences: list[Sentence] = []
    current = Sentence()
    for index, word in enumerate(words):
        current.words.append(word)
        ends_here = _TERMINAL.search(word.text) is not None
        if not ends_here and index + 1 < len(words):
            gap = words[index + 1].start - word.end
            too_long = word.end - current.start >= _MAX_SENTENCE_SECONDS
            ends_here = gap >= _SOFT_PAUSE_SECONDS and too_long
        if ends_here:
            sentences.append(current)
            current = Sentence()
    if current.words:
        sentences.append(current)
    return sentences


def load_sentences(transcript_path: Path) -> list[Sentence]:
    return build_sentences(load_words(transcript_path))
