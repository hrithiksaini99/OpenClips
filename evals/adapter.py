"""Bridge between the eval harness and the real clip selector.

The adapter is the only file that knows both sides. It loads a Whisper-shaped
fixture through the same code path the product uses (`load_sentences`), runs
`find_clips`, and records one span per selected clip so the graders can assert
on structure without re-deriving it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from agenteval.graders import GradeContext, register  # noqa: E402
from agenteval.models import AgentResult, Grade, Span  # noqa: E402
from studio.select import find_clips  # noqa: E402
from studio.transcript import load_sentences  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CLIP = "select_clip"
ALLOWED_OPTIONS = {"max_clips", "min_seconds", "max_seconds", "min_gap_seconds"}


def _clips(result: AgentResult) -> list[Span]:
    return [span for span in result.spans if span.name == CLIP]


# --------------------------------------------------------------------------- #
# domain graders
# --------------------------------------------------------------------------- #


@register("clip_count")
def _clip_count(spec: dict[str, Any], result: AgentResult, ctx: GradeContext) -> Grade:
    clips = _clips(result)
    low, high = int(spec.get("min", 0)), int(spec.get("max", 10**6))
    if low <= len(clips) <= high:
        return Grade("clip_count", True, 1.0, f"{len(clips)} clips within [{low}, {high}]")
    return Grade(
        "clip_count",
        False,
        0.0,
        f"{len(clips)} clips outside [{low}, {high}]",
        failure_class="answer_quality",
    )


@register("clip_durations")
def _clip_durations(spec: dict[str, Any], result: AgentResult, ctx: GradeContext) -> Grade:
    low = float(spec.get("min_seconds", 0.0))
    high = float(spec.get("max_seconds", 10**9))
    bad = [
        (round(span.input["duration"], 2), span.input["title"][:40])
        for span in _clips(result)
        if not low <= float(span.input["duration"]) <= high
    ]
    if not bad:
        return Grade("clip_durations", True, 1.0, f"every clip within [{low}, {high}]s")
    return Grade(
        "clip_durations",
        False,
        0.0,
        f"out of bounds {bad!r}",
        failure_class="answer_quality",
    )


@register("clips_do_not_overlap")
def _clips_do_not_overlap(
    spec: dict[str, Any], result: AgentResult, ctx: GradeContext
) -> Grade:
    gap = float(spec.get("min_gap_seconds", 0.0))
    ordered = sorted(_clips(result), key=lambda span: float(span.input["start"]))
    for previous, current in zip(ordered, ordered[1:]):
        if float(current.input["start"]) < float(previous.input["end"]) + gap:
            return Grade(
                "clips_do_not_overlap",
                False,
                0.0,
                f"{previous.input['title'][:30]!r} and {current.input['title'][:30]!r} "
                f"are closer than {gap}s",
                failure_class="answer_quality",
            )
    return Grade("clips_do_not_overlap", True, 1.0, f"all clips at least {gap}s apart")


@register("clips_ordered")
def _clips_ordered(spec: dict[str, Any], result: AgentResult, ctx: GradeContext) -> Grade:
    starts = [float(span.input["start"]) for span in _clips(result)]
    if starts == sorted(starts):
        return Grade("clips_ordered", True, 1.0, "clips are in playback order")
    return Grade(
        "clips_ordered",
        False,
        0.0,
        f"starts out of order: {starts!r}",
        failure_class="format_violation",
    )


@register("clips_terminated")
def _clips_terminated(
    spec: dict[str, Any], result: AgentResult, ctx: GradeContext
) -> Grade:
    unterminated = [
        span.input["title"][:40] for span in _clips(result) if not span.input["terminated"]
    ]
    if not unterminated:
        return Grade("clips_terminated", True, 1.0, "every clip ends on terminal punctuation")
    return Grade(
        "clips_terminated",
        False,
        0.0,
        f"clips end mid-thought: {unterminated!r}",
        failure_class="answer_quality",
    )


@register("clips_titled")
def _clips_titled(spec: dict[str, Any], result: AgentResult, ctx: GradeContext) -> Grade:
    empty = [span.input["start"] for span in _clips(result) if not span.input["title"].strip()]
    if not empty:
        return Grade("clips_titled", True, 1.0, "every clip has a title")
    return Grade(
        "clips_titled", False, 0.0, f"untitled clips at {empty!r}", failure_class="format_violation"
    )


@register("clips_min_score")
def _clips_min_score(spec: dict[str, Any], result: AgentResult, ctx: GradeContext) -> Grade:
    floor = float(spec["value"])
    weak = [
        (round(float(span.input["score"]), 2), span.input["title"][:40])
        for span in _clips(result)
        if float(span.input["score"]) < floor
    ]
    if not weak:
        return Grade("clips_min_score", True, 1.0, f"every clip scores at least {floor}")
    return Grade(
        "clips_min_score",
        False,
        0.0,
        f"below floor {weak!r}",
        failure_class="answer_quality",
    )


# --------------------------------------------------------------------------- #
# the agent
# --------------------------------------------------------------------------- #


def run(fixture: str, context: dict[str, Any] | None = None) -> AgentResult:
    """Run the real selector over a fixture and report the clips it chose."""
    options = dict((context or {}).get("options") or {})
    unknown = set(options) - ALLOWED_OPTIONS
    if unknown:
        raise ValueError(f"unknown selection options: {sorted(unknown)}")

    sentences = load_sentences(FIXTURES / f"{fixture}.json")
    clips = find_clips(sentences, **options)

    spans = [
        Span(
            type="tool_call",
            name=CLIP,
            input={
                "start": round(clip.start, 3),
                "end": round(clip.end, 3),
                "duration": round(clip.duration, 3),
                "score": clip.score,
                "title": clip.title,
                "terminated": sentences[clip.last_sentence].is_terminated,
            },
            output={"first_sentence": clip.first_sentence, "last_sentence": clip.last_sentence},
        )
        for clip in clips
    ]

    if clips:
        output = "\n\n".join(
            f"[{index}] {clip.title}\n{clip.text}" for index, clip in enumerate(clips, start=1)
        )
    else:
        output = "(no clips selected)"

    return AgentResult(output=output, spans=spans)
