"""Golden-path tests for deterministic clip candidate selection."""

import pytest

from openclips.application.selection import build_candidates, score_segment
from openclips.domain.selection import SelectionBounds
from openclips.domain.transcripts import (
    TranscriptDocument,
    TranscriptSegment,
    TranscriptWord,
)


def _word(text: str, start: float, end: float) -> TranscriptWord:
    return TranscriptWord(text=text, start=start, end=end, probability=0.95)


def _segment(start: float, end: float, text: str) -> TranscriptSegment:
    words = []
    tokens = [token for token in text.split() if token]
    slot = (end - start) / max(len(tokens), 1)
    for offset, token in enumerate(tokens):
        word_start = start + offset * slot
        words.append(_word(token.strip(".,!?;:"), word_start, word_start + slot * 0.9))
    return TranscriptSegment(start=start, end=end, text=text, words=tuple(words))


def _document(
    segments: list[TranscriptSegment], duration: float | None = None
) -> TranscriptDocument:
    resolved = duration if duration is not None else segments[-1].end if segments else 0.0
    return TranscriptDocument(language="en", duration=resolved, segments=tuple(segments))


@pytest.fixture
def long_document() -> TranscriptDocument:
    """A 10-segment transcript where segment 1 and 5 are the strongest hooks."""
    texts = [
        "Welcome to the show everybody.",
        "The biggest mistake everyone makes with money is never tracking it.",
        "Let me explain how budgeting works in practice.",
        "First you list your expenses on a sheet.",
        "This one secret trick doubled my savings rate instantly!",
        "Next we compare high yield accounts.",
        "Rates change all the time though.",
        "Then automate transfers every payday.",
        "Discipline beats motivation every single time.",
        "Thanks for watching the episode today.",
    ]
    segments = [
        _segment(index * 15.0, (index + 1) * 15.0, text) for index, text in enumerate(texts)
    ]
    return _document(segments)


def test_scores_reward_hooks_and_rate(long_document: TranscriptDocument) -> None:
    hook = score_segment(4, long_document)
    plain = score_segment(8, long_document)

    assert hook > plain
    assert hook >= 2.25
    assert plain >= 1.0


def test_candidates_respect_bounds_without_forcing_count(
    long_document: TranscriptDocument,
) -> None:
    bounds = SelectionBounds(max_clips=3, min_duration_seconds=20.0, max_duration_seconds=90.0)

    candidates = build_candidates(long_document, bounds)

    assert len(candidates) <= 3
    assert candidates, "a coherent transcript should produce at least one candidate"
    for candidate in candidates:
        assert candidate.duration <= 90.0 + 1e-6


def test_selection_is_reproducible(long_document: TranscriptDocument) -> None:
    first = build_candidates(long_document, SelectionBounds())
    second = build_candidates(long_document, SelectionBounds())

    assert first == second


def test_short_transcript_produces_no_candidates() -> None:
    document = _document([_segment(0.0, 10.0, "Too short to clip anything useful.")])

    assert build_candidates(document, SelectionBounds()) == ()


def test_empty_transcript_produces_no_candidates() -> None:
    document = TranscriptDocument(language="en", duration=0.0, segments=())

    assert build_candidates(document, SelectionBounds()) == ()


def test_dead_air_is_trimmed_from_span_edges() -> None:
    leading_gap = _segment(0.0, 25.0, "Okay so here is the plan.")
    # Rebuild the lead segment with a silent gap before its words.
    spoken = list(leading_gap.words)
    shifted = tuple(
        TranscriptWord(
            text=word.text,
            start=word.start + 5.0,
            end=word.end + 5.0,
            probability=word.probability,
        )
        for word in spoken
    )
    gappy_lead = TranscriptSegment(
        start=0.0, end=30.0, text="Okay so here is the plan.", words=shifted
    )
    body = _segment(30.0, 60.0, "The biggest secret is showing up daily.")
    document = _document([gappy_lead, body])

    candidates = build_candidates(document, SelectionBounds())

    # Both segments individually satisfy the minimum duration.
    assert len(candidates) == 2
    assert candidates[0].start == pytest.approx(shifted[0].start)
    assert candidates[0].title.startswith("Okay")


def test_max_clips_limit_is_enforced(long_document: TranscriptDocument) -> None:
    bounds = SelectionBounds(max_clips=3)

    candidates = build_candidates(long_document, bounds)

    assert len(candidates) <= bounds.max_clips


def test_minimum_duration_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="max_clips"):
        SelectionBounds(max_clips=2)
    with pytest.raises(ValueError, match="below"):
        SelectionBounds(min_duration_seconds=50.0, max_duration_seconds=40.0)
