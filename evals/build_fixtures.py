"""Rebuild the transcript fixtures used by the eval suite.

The fixtures are committed JSON so a suite run never depends on this script.
This exists so the fixtures are reviewable as prose rather than as 800 lines of
word timings, and so changing an episode is a readable diff.

    python evals/build_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
WORDS_PER_SECOND = 2.8
SENTENCE_GAP_SECONDS = 0.35

# A well-formed episode: strong early hook, a stronger one two thirds in, and
# enough ordinary material between them for greedy selection to have to choose.
HOOK_HEAVY = [
    "Welcome back to the show, today we are talking about why good products still fail.",
    "I spent the last three years watching early stage teams make the same expensive mistakes.",
    "The biggest mistake founders make is hiring for passion instead of proof of work.",
    "That single decision quietly destroys more companies than bad pricing ever will.",
    "Here is the thing about hiring, you cannot interview your way to certainty.",
    "You can only watch what someone has already shipped under pressure.",
    "So we started asking every candidate for one artifact and one failure story.",
    "The artifact shows craft and the story shows whether they can learn.",
    "Most people think the answer is more process, and that is exactly backwards.",
    "Process is what you add when you do not trust the people you hired.",
    "If you hire well, the process can stay small and the team stays fast.",
    "We ran this experiment across eleven teams and the results were uncomfortable.",
    "Seven of those teams shipped faster after we removed two of their weekly meetings.",
    "The other four got slower, and every one of them had a staffing problem.",
    "Nobody tells you that the real reason startups die is running out of time, not money.",
    "You can always raise more money, you cannot raise more months of your life.",
    "That framing changed how we plan every quarter after that conversation.",
    "We now ask what we would cut if we had half the time and the same ambition.",
    "The answer is usually the thing nobody wants to own, which is why it survives.",
    "Look at the roadmap and find the project with no clear owner and no clear metric.",
    "That project is not a priority, it is a hiding place for a decision.",
    "Kill it and watch how much faster the rest of the quarter moves.",
    "I want to be clear that this is not an argument against planning.",
    "It is an argument against planning things you have already decided not to do.",
    "Next week we are going to walk through the actual hiring scorecard we use.",
    "Thanks for listening, and if this was useful, send it to one founder who needs it.",
]

# Rambling and hedged throughout. The selector still has to return something,
# but nothing here deserves to be called a hook.
FILLER_HEAVY = [
    "Um, so, you know, I guess we should probably start talking about the thing.",
    "Yeah, I mean, like, it is kind of a long story, you know what I mean.",
    "So basically, um, we were sort of working on this, like, project for a while.",
    "And, uh, I do not really remember, like, exactly when it started, to be honest.",
    "You know, it was just sort of, um, one of those things that happens, I guess.",
    "I mean, like, nobody really knew what we were doing, you know.",
    "So, uh, we kind of just, like, kept going and, you know, sort of hoped.",
    "And then, I mean, basically nothing happened for, like, a really long time.",
    "Um, so, yeah, that was sort of the, you know, the whole situation, I guess.",
    "I mean, like, we could talk about it more, but, uh, there is not much else.",
    "You know, it is kind of funny, sort of, in a way, looking back on it now.",
    "So, basically, um, I think we learned, like, a little bit, maybe, kind of.",
    "And, uh, you know, it was just one of those things, I mean, really.",
    "Like, I do not know, maybe it was, sort of, the wrong time, you know.",
    "Um, so, yeah, anyway, that is, like, basically the story, I guess.",
    "I mean, you know, we could, uh, sort of try it again at some point.",
    "But, like, honestly, I am not really sure, you know, if that makes sense.",
    "So, yeah, um, that is kind of it, I guess, thanks for, you know, listening.",
]

# Too short to contain a legally-sized clip. The correct answer is zero.
SHORT = [
    "This is a very short recording about a single idea.",
    "The idea is that small experiments beat large plans.",
    "That is the entire episode, thanks for listening.",
    "We will be back next week with something longer.",
]

EPISODES = {
    "hook-heavy": HOOK_HEAVY,
    "filler-heavy": FILLER_HEAVY,
    "short": SHORT,
}


def build_episode(sentences: list[str], *, wps: float = WORDS_PER_SECOND) -> dict:
    """Emit a Whisper-shaped document: one segment per sentence, word timings inside."""
    segments = []
    cursor = 0.0
    for text in sentences:
        tokens = text.split()
        duration = len(tokens) / wps
        slot = duration / len(tokens)
        words = []
        for index, token in enumerate(tokens):
            start = cursor + index * slot
            words.append(
                {
                    "text": token,
                    "start": round(start, 3),
                    "end": round(start + slot * 0.92, 3),
                    "probability": 0.96,
                }
            )
        segments.append(
            {
                "start": round(cursor, 3),
                "end": round(cursor + duration, 3),
                "text": text,
                "words": words,
            }
        )
        cursor += duration + SENTENCE_GAP_SECONDS
    return {"language": "en", "duration": round(cursor, 3), "segments": segments}


def render(payload: dict) -> str:
    """One segment per line: readable in review, cheap in the diff."""
    segments = payload["segments"]
    lines = [
        "{",
        f' "language": {json.dumps(payload["language"])},',
        f' "duration": {payload["duration"]},',
        ' "segments": [',
    ]
    for index, segment in enumerate(segments):
        comma = "," if index < len(segments) - 1 else ""
        lines.append("  " + json.dumps(segment, separators=(",", ":")) + comma)
    lines += [" ]", "}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, sentences in EPISODES.items():
        payload = build_episode(sentences)
        path = FIXTURES / f"{name}.json"
        path.write_text(render(payload), encoding="utf-8")
        print(f"{path.name}: {len(sentences)} sentences, {payload['duration']:.1f}s")


if __name__ == "__main__":
    main()
