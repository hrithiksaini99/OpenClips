"""Local clip-refinement provider contract and deterministic adapters."""

import json
from collections.abc import Callable, Sequence

from openclips.domain.selection import ClipCandidate, SelectionBounds
from openclips.domain.transcripts import TranscriptDocument


class MalformedModelOutputError(ValueError):
    """Raised when a refiner emits output that cannot be parsed or validated."""


class ClipRefiner:
    """Contract for providers that adjust candidate boundaries for coherence."""

    def refine(
        self,
        candidates: Sequence[ClipCandidate],
        document: TranscriptDocument,
        bounds: SelectionBounds,
    ) -> tuple[ClipCandidate, ...]:
        """Return adjusted candidates; implementations must be deterministic."""
        raise NotImplementedError


class HeuristicClipRefiner(ClipRefiner):
    """Baseline local refiner that keeps validated heuristic candidates as-is."""

    def refine(
        self,
        candidates: Sequence[ClipCandidate],
        document: TranscriptDocument,
        bounds: SelectionBounds,
    ) -> tuple[ClipCandidate, ...]:
        del document
        for candidate in candidates:
            if not (
                bounds.min_duration_seconds
                <= candidate.duration
                <= bounds.max_duration_seconds + 1e-6
            ):
                msg = f"Candidate {candidate.title!r} violates configured duration bounds"
                raise MalformedModelOutputError(msg)
        return tuple(candidates)


class LocalLlmClipRefiner(ClipRefiner):
    """Refines candidates through a local text model emitting strict JSON."""

    def __init__(self, *, complete: Callable[[str], str]) -> None:
        self._complete = complete

    def refine(
        self,
        candidates: Sequence[ClipCandidate],
        document: TranscriptDocument,
        bounds: SelectionBounds,
    ) -> tuple[ClipCandidate, ...]:
        raw = self._complete(_build_prompt(candidates))
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            msg = f"Model output is not valid JSON: {error}"
            raise MalformedModelOutputError(msg) from error
        return _parse_payload(payload, document, bounds)


def _build_prompt(candidates: Sequence[ClipCandidate]) -> str:
    lines = [
        "Adjust these clip boundaries so each is self-contained.",
        'Reply with JSON only: {"clips": [{"start": <seconds>, "end": <seconds>, '
        '"title": "<text>"}]}',
        "",
    ]
    for candidate in candidates:
        lines.append(f"- [{candidate.start:.2f}, {candidate.end:.2f}] {candidate.title}")
    return "\n".join(lines)


def _parse_payload(
    payload: object,
    document: TranscriptDocument,
    bounds: SelectionBounds,
) -> tuple[ClipCandidate, ...]:
    if not isinstance(payload, dict):
        msg = "Model output must be a JSON object"
        raise MalformedModelOutputError(msg)
    raw_clips = payload.get("clips")
    if not isinstance(raw_clips, list):
        msg = "Model output must contain a 'clips' array"
        raise MalformedModelOutputError(msg)
    if len(raw_clips) > bounds.max_clips:
        msg = f"Model proposed {len(raw_clips)} clips which exceeds the limit of {bounds.max_clips}"
        raise MalformedModelOutputError(msg)

    refined: list[ClipCandidate] = []
    for entry in raw_clips:
        if not isinstance(entry, dict):
            msg = "Each clip entry must be a JSON object"
            raise MalformedModelOutputError(msg)
        unexpected = set(entry) - {"start", "end", "title"}
        if unexpected:
            msg = f"Clip entry has unexpected fields: {sorted(unexpected)}"
            raise MalformedModelOutputError(msg)
        try:
            start = float(entry["start"])
            end = float(entry["end"])
            title = str(entry["title"])
        except (KeyError, TypeError, ValueError) as error:
            msg = f"Clip entry fields are invalid: {error}"
            raise MalformedModelOutputError(msg) from error
        if start < 0 or end <= start or end > document.duration + 1e-6:
            msg = f"Refined span [{start}, {end}] falls outside the transcript"
            raise MalformedModelOutputError(msg)
        if not (
            bounds.min_duration_seconds - 1e-6
            <= end - start
            <= bounds.max_duration_seconds + 1e-6
        ):
            msg = f"Refined span [{start}, {end}] violates duration bounds"
            raise MalformedModelOutputError(msg)
        refined.append(
            ClipCandidate(
                start=start,
                end=end,
                title=title.strip() or "Untitled clip",
                score=0.0,
                segment_indices=(),
            )
        )
    return tuple(refined)
