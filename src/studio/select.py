"""Clip selection: find self-contained, hook-first moments worth posting.

Candidates are always whole-sentence spans, so a clip opens on a real sentence
and closes on terminal punctuation. Scoring rewards a strong opening hook, a
dense payload, and a length in the short-form sweet spot, then greedy
non-overlapping selection keeps the best spread across the episode.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from studio.transcript import Sentence

MIN_SECONDS = 28.0
MAX_SECONDS = 75.0
IDEAL_SECONDS = 45.0

# Openers that make a viewer stay past the first second.
_HOOK_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"^(what|why|how|when|who|where)\b", 2.4),
    (r"^(so|and) (i|he|she|they|we) (was|were|had|got|went|found|realized)", 1.6),
    (r"\b(the (biggest|craziest|weirdest|scariest|most \w+)|number one)\b", 2.6),
    (r"\b(nobody|no one|everyone|everybody) (knows|realizes|talks about|tells you)\b", 2.8),
    (r"\b(here'?s the (thing|crazy part|problem)|the truth is|the reality is)\b", 2.6),
    (r"\b(most people|people think|they tell you|we're told)\b", 2.2),
    (r"\b(i (never|always)|you (never|always))\b", 1.4),
    (r"\b(imagine|picture this|think about (it|this))\b", 1.8),
    (r"^(one|a) (time|day|night|guy|woman|man|friend)\b", 1.6),
    (r"\b(turns out|it turned out)\b", 1.6),
)

# Substance markers: stakes, specificity, conflict, revelation.
_PAYLOAD_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"\b(million|billion|thousand|percent|\d{2,})\b", 0.9),
    (r"\b(died|death|killed|dangerous|illegal|arrested|prison|lawsuit)\b", 1.1),
    (r"\b(crazy|insane|wild|unbelievable|shocking|terrifying)\b", 0.8),
    (r"\b(discovered|realized|figured out|learned|proved|research|study|evidence)\b", 0.9),
    (r"\b(secret|hidden|nobody knows|classified|cover.?up)\b", 1.0),
    (r"\b(mistake|failed|failure|wrong|problem|struggle)\b", 0.7),
    (r"\b(money|business|invest|rich|broke|salary|profit)\b", 0.7),
    (r"\b(brain|body|health|sleep|diet|exercise|drug|medicine)\b", 0.6),
)

_FILLER = re.compile(r"\b(um|uh|you know|i mean|like|sort of|kind of|basically)\b")
_SENTENCE_SPLIT = re.compile(r"\s+")


@dataclass(frozen=True)
class ClipCandidate:
    start: float
    end: float
    title: str
    score: float
    text: str
    first_sentence: int
    last_sentence: int

    @property
    def duration(self) -> float:
        return self.end - self.start


def _pattern_score(text: str, patterns: tuple[tuple[str, float], ...], cap: float) -> float:
    total = 0.0
    for pattern, weight in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            total += weight
    return min(total, cap)


def _length_score(duration: float) -> float:
    """Bell curve peaking at the short-form sweet spot."""
    return math.exp(-((duration - IDEAL_SECONDS) ** 2) / (2 * 18.0**2))


def _filler_ratio(text: str) -> float:
    words = _SENTENCE_SPLIT.split(text.strip())
    if not words:
        return 1.0
    return len(_FILLER.findall(text.lower())) / len(words)


def score_window(sentences: list[Sentence], first: int, last: int) -> float:
    span = sentences[first : last + 1]
    text = " ".join(sentence.text for sentence in span)
    duration = span[-1].end - span[0].start
    if duration <= 0:
        return 0.0

    word_count = sum(len(sentence.words) for sentence in span)
    density = word_count / duration  # words per second; dead air scores low

    hook = _pattern_score(span[0].text, _HOOK_PATTERNS, cap=4.0)
    payload = _pattern_score(text, _PAYLOAD_PATTERNS, cap=4.0)
    completeness = 1.0 if span[-1].is_terminated else 0.35
    density_score = max(0.0, min(density / 3.2, 1.0))
    filler_penalty = min(_filler_ratio(text) * 4.0, 1.2)

    return (
        hook * 1.7
        + payload * 1.0
        + density_score * 2.2
        + _length_score(duration) * 2.6
        + completeness * 1.4
        - filler_penalty
    )


def _title_from(sentence: Sentence, limit: int = 68) -> str:
    text = sentence.text.strip()
    text = re.sub(r"^(so|and|but|well|you know|i mean)[,\s]+", "", text, flags=re.IGNORECASE)
    text = text[:1].upper() + text[1:]
    if len(text) <= limit:
        return text.rstrip(" ,;:")
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:") + "…"


def find_clips(
    sentences: list[Sentence],
    *,
    max_clips: int = 15,
    min_seconds: float = MIN_SECONDS,
    max_seconds: float = MAX_SECONDS,
    min_gap_seconds: float = 20.0,
) -> list[ClipCandidate]:
    """Score every whole-sentence window, then greedily take the best spread."""
    candidates: list[ClipCandidate] = []
    for first in range(len(sentences)):
        for last in range(first, len(sentences)):
            duration = sentences[last].end - sentences[first].start
            if duration < min_seconds:
                continue
            if duration > max_seconds:
                break
            score = score_window(sentences, first, last)
            span_text = " ".join(s.text for s in sentences[first : last + 1])
            candidates.append(
                ClipCandidate(
                    start=sentences[first].start,
                    end=sentences[last].end,
                    title=_title_from(sentences[first]),
                    score=round(score, 4),
                    text=span_text,
                    first_sentence=first,
                    last_sentence=last,
                )
            )

    candidates.sort(key=lambda candidate: (-candidate.score, candidate.start))
    chosen: list[ClipCandidate] = []
    for candidate in candidates:
        if len(chosen) >= max_clips:
            break
        conflict = any(
            candidate.start < taken.end + min_gap_seconds
            and taken.start - min_gap_seconds < candidate.end
            for taken in chosen
        )
        if not conflict:
            chosen.append(candidate)
    chosen.sort(key=lambda candidate: candidate.start)
    return chosen
