"""Local LLM ranking of clip candidates through Ollama.

The heuristic selector is good at finding well-formed, energetic, hook-shaped
spans, but it cannot tell whether a moment is actually *interesting*. This layer
hands the shortlist to a local model and asks the question the heuristics
cannot: would this hold someone's attention on its own?

Everything here degrades to "no opinion" rather than failing the run: if Ollama
is not running, the model is missing, or the reply does not fit the schema, the
caller keeps the heuristic ordering.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_HOST = os.environ.get("OPENCLIPS_OLLAMA_HOST", "http://localhost:11434")
# Overridable because the deployment target is a 4 GB-VRAM RTX 3050, where a
# smaller Gemma is used than on a development machine with unified memory.
DEFAULT_MODEL = os.environ.get("OPENCLIPS_LLM_MODEL", "gemma4:latest")

# Ollama enforces this as a grammar, which is what makes the reply parseable;
# asking for JSON in the prompt alone returned empty completions.
_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "score": {"type": "integer"},
                    "title": {"type": "string"},
                },
                "required": ["id", "score", "title"],
            },
        }
    },
    "required": ["picks"],
}

def model_available(model: str, host: str = DEFAULT_HOST) -> bool:
    """True when Ollama is up and `model` is installed."""
    try:
        with urllib.request.urlopen(f"{host.rstrip('/')}/api/tags", timeout=5) as response:
            tags = json.loads(response.read())
    except Exception:
        return False
    names = {entry.get("name", "") for entry in tags.get("models", [])}
    stem = model.split(":")[0]
    return any(name == model or name.split(":")[0] == stem for name in names)


def generate_json(
    *,
    prompt: str,
    schema: dict,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    timeout: float = 300.0,
    options: dict | None = None,
) -> dict:
    """Ask Ollama for one JSON object shaped by `schema`.

    The schema is passed as Ollama's `format` parameter, which it enforces as a
    grammar; asking for JSON in the prompt alone returned empty completions.
    """
    body = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": schema,
            "options": options or {"temperature": 0.2},
        }
    ).encode()
    request = urllib.request.Request(
        f"{host.rstrip('/')}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    return json.loads(payload["response"])


_INSTRUCTIONS = """You choose moments from a podcast to post as standalone short videos.

Score each excerpt from 0 to 100 for how well it works alone:
90-100  opens on a real hook, one complete surprising idea, needs no context
60-89   interesting and self-contained, weaker opening
30-59   understandable but ordinary, or leans on missing context
0-29    rambling, filler, or meaningless without the rest of the episode

Then write a title that is the excerpt's own hook in its own words, under 60
characters, no hype words you did not hear, no quotation marks.

Reply with one entry per excerpt, using the excerpt's number as "id"."""


@dataclass(frozen=True)
class Ranking:
    index: int
    score: float  # normalised to 0-100
    title: str


class ClipRanker:
    """Scores candidate excerpts with a local Ollama model."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        timeout: float = 300.0,
        batch_size: int = 8,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        # Small batches keep the prompt short, which matters on a 4 GB card.
        self.batch_size = max(1, batch_size)

    def available(self) -> bool:
        """True when Ollama is up and the configured model is installed."""
        return model_available(self.model, self.host)

    def rank(self, excerpts: list[str]) -> list[Ranking]:
        """Score every excerpt, returning whatever the model answered cleanly."""
        rankings: list[Ranking] = []
        for offset in range(0, len(excerpts), self.batch_size):
            batch = excerpts[offset : offset + self.batch_size]
            try:
                rankings.extend(self._rank_batch(batch, offset))
            except Exception:
                continue  # a bad batch loses its opinions, not the whole run
        return rankings

    def _rank_batch(self, batch: list[str], offset: int) -> list[Ranking]:
        numbered = "\n\n".join(
            f"[{index + 1}] {text[:900]}" for index, text in enumerate(batch)
        )
        payload = generate_json(
            prompt=f"{_INSTRUCTIONS}\n\n{numbered}",
            schema=_SCHEMA,
            model=self.model,
            host=self.host,
            timeout=self.timeout,
            options={"temperature": 0.2, "num_predict": 120 * len(batch) + 200},
        )
        picks = payload.get("picks", [])

        raw = [
            (int(pick["id"]), float(pick["score"]), str(pick.get("title", "")).strip())
            for pick in picks
            if isinstance(pick, dict) and "id" in pick and "score" in pick
        ]
        # Models drift to a familiar 1-5 or 1-10 scale despite the instruction;
        # rescale rather than treating a 5 as "almost worthless".
        highest = max((score for _id, score, _title in raw), default=0.0)
        factor = 20.0 if highest <= 5 else (10.0 if highest <= 10 else 1.0)

        rankings: list[Ranking] = []
        for identifier, score, title in raw:
            index = offset + identifier - 1
            if 0 <= index - offset < len(batch):
                rankings.append(
                    Ranking(
                        index=index,
                        score=max(0.0, min(score * factor, 100.0)),
                        title=title[:70],
                    )
                )
        return rankings
