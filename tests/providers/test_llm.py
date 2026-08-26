"""Contract tests for the local LLM clip refiner interface."""

import pytest

from openclips.domain.selection import ClipCandidate, SelectionBounds
from openclips.domain.transcripts import TranscriptDocument
from openclips.providers.llm import (
    ClipRefiner,
    HeuristicClipRefiner,
    LocalLlmClipRefiner,
    MalformedModelOutputError,
)


def _candidate(start: float = 0.0, end: float = 30.0) -> ClipCandidate:
    return ClipCandidate(
        start=start,
        end=end,
        title="Seed candidate",
        score=2.0,
        segment_indices=(0,),
    )


@pytest.fixture
def document() -> TranscriptDocument:
    return TranscriptDocument(language="en", duration=120.0, segments=())


def test_refiner_contract_raises_without_implementation(document: TranscriptDocument) -> None:
    with pytest.raises(NotImplementedError):
        ClipRefiner().refine([], document, SelectionBounds())


def test_heuristic_refiner_passes_valid_candidates_through(
    document: TranscriptDocument,
) -> None:
    candidates = (_candidate(), _candidate(40.0, 80.0))

    refined = HeuristicClipRefiner().refine(candidates, document, SelectionBounds())

    assert refined == candidates


def test_heuristic_refiner_rejects_out_of_bounds_candidates(
    document: TranscriptDocument,
) -> None:
    oversized = _candidate(0.0, 120.0)

    with pytest.raises(MalformedModelOutputError, match="duration bounds"):
        HeuristicClipRefiner().refine([oversized], document, SelectionBounds())


def test_local_llm_refiner_parses_strict_json(
    document: TranscriptDocument,
) -> None:
    def complete(prompt: str) -> str:
        del prompt
        return '{"clips": [{"start": 10.0, "end": 50.0, "title": "Money mistake"}]}'

    refined = LocalLlmClipRefiner(complete=complete).refine(
        [_candidate()], document, SelectionBounds()
    )

    assert len(refined) == 1
    assert refined[0].title == "Money mistake"
    assert refined[0].start == pytest.approx(10.0)


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '{"segments": []}',
        '{"clips": {"start": 1.0}}',
        '{"clips": [{"start": "soon", "end": 40.0, "title": "x"}]}',
        '{"clips": [{"start": 0.0, "end": 400.0, "title": "too long"}]}',
        '{"clips": [{"start": 0.0, "end": 30.0, "title": "ok", "score": 9}]}',
        '[{"start": 0.0}]',
        '{"clips": [{"start": -5.0, "end": 30.0, "title": "negative"}]}',
        '{"clips": [{"start": 20.0, "end": 10.0, "title": "inverted"}]}',
    ],
)
def test_malformed_model_output_is_rejected_safely(
    raw: str, document: TranscriptDocument
) -> None:
    refiner = LocalLlmClipRefiner(complete=lambda prompt: raw)

    with pytest.raises(MalformedModelOutputError):
        refiner.refine([_candidate()], document, SelectionBounds())


def test_excessive_clip_count_is_rejected(document: TranscriptDocument) -> None:
    entries = ",".join(
        f'{{"start": {index * 25.0}, "end": {index * 25.0 + 24.0}, "title": "c"}}'
        for index in range(12)
    )
    refiner = LocalLlmClipRefiner(complete=lambda prompt: f'{{"clips": [{entries}]}}')

    with pytest.raises(MalformedModelOutputError, match="exceeds"):
        refiner.refine([_candidate()], document, SelectionBounds(max_clips=3))
