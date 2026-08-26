import pytest

from openclips.domain.transcripts import (
    TranscriptDocument,
    TranscriptSegment,
    TranscriptWord,
)


def test_word_rejects_inverted_timing() -> None:
    with pytest.raises(ValueError, match="precedes"):
        TranscriptWord(text="hi", start=2.0, end=1.0, probability=0.9)


def test_word_rejects_probability_outside_unit_range() -> None:
    with pytest.raises(ValueError, match="probability"):
        TranscriptWord(text="hi", start=1.0, end=2.0, probability=1.5)


def test_segment_rejects_inverted_timing() -> None:
    with pytest.raises(ValueError, match="precedes"):
        TranscriptSegment(start=5.0, end=4.0, text="hi", words=())


def test_document_rejects_negative_duration() -> None:
    with pytest.raises(ValueError, match="negative"):
        TranscriptDocument(language="en", duration=-1.0, segments=())


def test_document_aggregates_words_and_text() -> None:
    words = (
        TranscriptWord(text="hello", start=0.0, end=0.5, probability=0.9),
        TranscriptWord(text="world", start=0.5, end=1.0, probability=0.8),
    )
    segment = TranscriptSegment(start=0.0, end=1.0, text=" hello world ", words=words)
    document = TranscriptDocument(language="en", duration=1.0, segments=(segment,))

    assert document.word_count == 2
    assert document.full_text == "hello world"
