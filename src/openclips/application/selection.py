"""Deterministic transcript-first clip candidate selection."""

from openclips.domain.selection import ClipCandidate, SelectionBounds
from openclips.domain.transcripts import TranscriptDocument

_HOOK_TERMS = frozenset(
    {
        "how",
        "why",
        "what",
        "secret",
        "secrets",
        "mistake",
        "mistakes",
        "never",
        "always",
        "best",
        "worst",
        "biggest",
        "insane",
        "crazy",
        "truth",
        "nobody",
        "everyone",
        "money",
        "free",
        "hack",
        "trick",
    }
)
_IDEAL_WORDS_PER_SECOND = 2.5
_DEAD_AIR_GAP_SECONDS = 0.8
_TITLE_WORD_BUDGET = 8


def _words_of(text: str) -> list[str]:
    return [token.strip(".,!?;:").lower() for token in text.split() if token.strip(".,!?;:")]


def score_segment(index: int, document: TranscriptDocument) -> float:
    """Score one segment on hook density and speech rate between 1 and 3."""
    segment = document.segments[index]
    words = _words_of(segment.text)
    if not words:
        return 0.0
    hooks = sum(1 for word in words if word in _HOOK_TERMS)
    if segment.text.rstrip().endswith(("?", "!")):
        hooks += 1
    hook_score = min(hooks / 3.0, 1.0)
    rate = len(words) / max(segment.duration, 0.1)
    rate_score = min(rate / _IDEAL_WORDS_PER_SECOND, 1.0)
    return round(1.0 + 1.25 * hook_score + 0.75 * rate_score, 4)


def _span_bounds(
    start: int, end: int, document: TranscriptDocument
) -> tuple[float, float]:
    return document.segments[start].start, document.segments[end].end


def _span_duration(start: int, end: int, document: TranscriptDocument) -> float:
    span_start, span_end = _span_bounds(start, end, document)
    return span_end - span_start


def _grow_to_minimum(
    seed: int, document: TranscriptDocument, bounds: SelectionBounds
) -> tuple[int, int]:
    """Absorb the higher-scored neighbor until the span meets the minimum duration."""
    start, end = seed, seed
    last_index = len(document.segments) - 1
    while (
        _span_duration(start, end, document) < bounds.min_duration_seconds
        and (start > 0 or end < last_index)
    ):
        grow_left = start > 0 and (
            end == last_index
            or score_segment(start - 1, document) >= score_segment(end + 1, document)
        )
        if grow_left:
            start -= 1
        else:
            end += 1
    return start, end


def _shrink_to_maximum(
    start: int,
    end: int,
    scores: dict[int, float],
    document: TranscriptDocument,
    bounds: SelectionBounds,
) -> tuple[int, int]:
    """Drop the lower-scored edge segment until the span fits the maximum."""
    while _span_duration(start, end, document) > bounds.max_duration_seconds and start != end:
        left_score = scores[start]
        right_score = scores[end]
        if left_score <= right_score:
            start += 1
        else:
            end -= 1
    return start, end


def _trim_single_oversize_span(
    start: int,
    end: int,
    document: TranscriptDocument,
    bounds: SelectionBounds,
) -> tuple[float, float] | None:
    """Cut an oversized unsplittable span at the last word boundary that fits."""
    if end != start:
        return None
    segment = document.segments[start]
    deadline = segment.start + bounds.max_duration_seconds
    fitting = [word for word in segment.words if word.end <= deadline]
    if fitting:
        return round(segment.start, 3), round(fitting[-1].end, 3)
    return round(segment.start, 3), round(deadline, 3)


def _trim_dead_air(
    document: TranscriptDocument, indices: range
) -> tuple[float, float] | None:
    """Snap the span inward past leading or trailing gaps of silent words."""
    segments = [document.segments[index] for index in indices]
    words = [word for segment in segments for word in segment.words]
    if not words:
        return None
    start = segments[0].start
    end = segments[-1].end

    lead = 0
    while (
        lead < len(words) - 1
        and words[lead + 1].start - words[lead].end >= _DEAD_AIR_GAP_SECONDS
    ):
        lead += 1
    tail = len(words) - 1
    while tail > 0 and words[tail].start - words[tail - 1].end >= _DEAD_AIR_GAP_SECONDS:
        tail -= 1
    trimmed_start, trimmed_end = words[lead].start, words[tail].end
    if trimmed_end <= trimmed_start:
        return round(start, 3), round(end, 3)
    return round(trimmed_start, 3), round(trimmed_end, 3)


def _derive_title(document: TranscriptDocument, indices: range) -> str:
    text = " ".join(document.segments[index].text for index in indices)
    words: list[str] = []
    for token in text.split():
        cleaned = token.strip(".,!?;:")
        if cleaned:
            words.append(cleaned)
        if len(words) >= _TITLE_WORD_BUDGET:
            break
    title = " ".join(words).strip() or "Untitled clip"
    return title[0].upper() + title[1:]


def build_candidates(
    document: TranscriptDocument,
    bounds: SelectionBounds | None = None,
) -> tuple[ClipCandidate, ...]:
    """Select non-overlapping bounded candidates without forcing the clip count."""
    resolved_bounds = bounds or SelectionBounds()
    if not document.segments:
        return ()
    if document.duration < resolved_bounds.min_duration_seconds:
        return ()

    scores = {index: score_segment(index, document) for index in range(len(document.segments))}
    seeds = sorted(scores, key=lambda index: (-scores[index], index))

    candidates: list[ClipCandidate] = []
    for seed in seeds:
        if len(candidates) >= resolved_bounds.max_clips:
            break
        start, end = _grow_to_minimum(seed, document, resolved_bounds)
        if _span_duration(start, end, document) > resolved_bounds.max_duration_seconds:
            start, end = _shrink_to_maximum(start, end, scores, document, resolved_bounds)

        if _span_duration(start, end, document) > resolved_bounds.max_duration_seconds:
            span = _trim_single_oversize_span(start, end, document, resolved_bounds)
            indices_range: range | None = None
        else:
            span = _trim_dead_air(document, range(start, end + 1))
            indices_range = range(start, end + 1)
        if span is None:
            continue

        if indices_range is None:
            title_indices: range = range(start, end + 1)
        else:
            title_indices = indices_range
        member_scores = [scores[index] for index in title_indices]
        average_score = round(sum(member_scores) / len(member_scores), 4)

        candidate = ClipCandidate(
            start=span[0],
            end=span[1],
            title=_derive_title(document, title_indices),
            score=average_score,
            segment_indices=tuple(title_indices),
        )
        if any(candidate.overlaps(existing) for existing in candidates):
            continue
        candidates.append(candidate)

    return tuple(sorted(candidates, key=lambda candidate: candidate.start))
