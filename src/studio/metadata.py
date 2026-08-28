"""Gemma writes the post for one clip: title, description, hashtags.

Same shape as ClipRanker in llm.py — a JSON schema handed to Ollama as a
grammar, small prompts, and a fall back to the clip's own heuristic title when
the model is not there.

The limits are enforced here rather than trusted from the prompt. YouTube
rejects an entire upload over a title one character too long, and a local model
asked politely for "under 80 characters" will hand back 96 often enough to
matter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from studio.llm import DEFAULT_HOST, DEFAULT_MODEL, generate_json, model_available

# YouTube's own hard limits, not preferences.
TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 5000
TAG_BUDGET = 500  # total characters across all keyword tags
# YouTube ignores a video's hashtags past the fifteenth.
MAX_HASHTAGS = 15

_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "description", "hashtags"],
}

_INSTRUCTIONS = """You write the YouTube post for one short vertical video cut from a podcast.

title: 40 to 80 characters. Say what happens in the clip so that it makes sense
to somebody who has not watched it, naming the person, thing or claim involved.
Do not copy a sentence out of the transcript and do not begin with "I", "he",
"she" or "they". No quotation marks, no emoji, no episode numbers, and no hype
words you cannot hear in the transcript.

description: two or three sentences. The first says what actually happens in
the clip. The rest adds only context a viewer needs to follow it. Do not invent
names, numbers or claims that are not in the transcript.

hashtags: between five and ten, lowercase, each one relevant to what is really
discussed. No reach-bait like viral, fyp, trending or explore.

Transcript of the clip:"""

_TAG_CLEAN = re.compile(r"[^0-9a-z]+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class PostMetadata:
    title: str
    description: str
    hashtags: tuple[str, ...]

    def tags(self) -> list[str]:
        """Hashtags as YouTube keyword tags, inside the 500-character budget.

        The budget covers every tag together, so this fills up to it and stops
        rather than sending a list the API will reject wholesale.
        """
        chosen: list[str] = []
        spent = 0
        for tag in self.hashtags:
            word = tag.lstrip("#")
            if spent + len(word) > TAG_BUDGET:
                break
            chosen.append(word)
            spent += len(word)
        return chosen


def _flatten(text: str) -> str:
    """Lowercase, strip punctuation, collapse spaces — for comparing wording."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", str(text).lower()).split())


def is_quote(title: str, transcript: str) -> bool:
    """True when the title is just a line lifted out of the clip.

    A sentence copied from the transcript reads as a caption, not a title: it
    usually opens on a pronoun and means nothing to somebody scrolling past.
    The prompt asks the model not to do this and it mostly complies, so this is
    the backstop rather than the mechanism.
    """
    flat = _flatten(title)
    return bool(flat) and flat in _flatten(transcript)


def clamp_title(text: str, fallback: str, transcript: str = "") -> str:
    """A title that fits, without cutting a word in half.

    Falls back to the heuristic title when the model returns something too
    short to be a sentence or copied straight out of the clip.
    """
    cleaned = " ".join(str(text).replace("\n", " ").split()).strip('"“”')
    if len(cleaned) < 16 or (transcript and is_quote(cleaned, transcript)):
        cleaned = " ".join(str(fallback).split())
    if len(cleaned) <= TITLE_LIMIT:
        return cleaned or "Clip"
    trimmed = cleaned[:TITLE_LIMIT]
    head, _, _tail = trimmed.rpartition(" ")
    return (head or trimmed).rstrip(" ,;:-") or "Clip"


def normalise_hashtags(raw: object, limit: int = MAX_HASHTAGS) -> tuple[str, ...]:
    """Lowercase, deduplicate and strip punctuation from whatever came back."""
    if not isinstance(raw, (list, tuple)):
        return ()
    seen: set[str] = set()
    tags: list[str] = []
    for item in raw:
        word = _TAG_CLEAN.sub("", str(item).lower().lstrip("#"))
        if len(word) < 2 or word in seen:
            continue
        seen.add(word)
        tags.append(f"#{word}")
        if len(tags) >= limit:
            break
    return tuple(tags)


def _credit(source_title: str, source_url: str) -> str:
    """The attribution line.

    These clips are cut from someone else's episode. Naming the source is the
    decent thing, and it is also what an API audit looks for.
    """
    if not source_title and not source_url:
        return ""
    line = f"Clipped from: {source_title}" if source_title else "Clipped from the original episode"
    return f"{line}\n{source_url}" if source_url else line


def compose_description(
    body: str,
    *,
    hashtags: tuple[str, ...] = (),
    source_title: str = "",
    source_url: str = "",
) -> str:
    """Assemble the final description: the model's text, hashtags, attribution."""
    blocks = [" ".join(str(body).split())]
    if hashtags:
        blocks.append(" ".join(hashtags))
    blocks.append(_credit(source_title, source_url))
    return "\n\n".join(block for block in blocks if block)[:DESCRIPTION_LIMIT]


def _fallback_body(text: str) -> str:
    """First couple of sentences of the clip, for when the model is unavailable."""
    sentences = _SENTENCE_END.split(" ".join(str(text).split()))
    return " ".join(sentences[:2])[:400]


class PostWriter:
    """Writes the YouTube post for a clip with a local Ollama model."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        timeout: float = 180.0,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout

    def available(self) -> bool:
        return model_available(self.model, self.host)

    def write(
        self,
        *,
        text: str,
        fallback_title: str,
        source_title: str = "",
        source_url: str = "",
    ) -> PostMetadata:
        """Write one post, degrading to the clip's own title if the model fails.

        A missing Ollama is not a reason to lose a clip from the queue, so every
        failure path here still returns something postable.
        """
        try:
            payload = generate_json(
                prompt=f"{_INSTRUCTIONS}\n\n{text[:3000]}",
                schema=_SCHEMA,
                model=self.model,
                host=self.host,
                timeout=self.timeout,
                options={"temperature": 0.3, "num_predict": 500},
            )
        except Exception:
            payload = {}

        hashtags = normalise_hashtags(payload.get("hashtags"))
        body = str(payload.get("description", "")).strip() or _fallback_body(text)
        return PostMetadata(
            title=clamp_title(payload.get("title", ""), fallback_title, text),
            description=compose_description(
                body,
                hashtags=hashtags,
                source_title=source_title,
                source_url=source_url,
            ),
            hashtags=hashtags,
        )
