"""The post Gemma writes, and the limits it is held to.

YouTube rejects an entire upload over a title one character too long, so the
limits are enforced in code rather than asked for in the prompt. A local model
asked politely for "under 80 characters" returns 96 often enough to matter.
"""

from __future__ import annotations

from studio.metadata import (
    DESCRIPTION_LIMIT,
    MAX_HASHTAGS,
    TAG_BUDGET,
    TITLE_LIMIT,
    PostMetadata,
    clamp_title,
    compose_description,
    is_quote,
    normalise_hashtags,
)


def test_a_long_title_is_cut_to_the_limit() -> None:
    assert len(clamp_title("word " * 60, "fallback")) <= TITLE_LIMIT


def test_a_title_is_never_cut_mid_word() -> None:
    title = clamp_title("supercalifragilistic " * 8, "fallback")

    assert not title.endswith("-")
    assert "supercalifragilisti " not in title


def test_a_title_lifted_from_the_transcript_falls_back() -> None:
    # The model kept returning lines like "I don't see jack shit", which is a
    # caption, not a title: it opens on a pronoun and means nothing in a feed.
    transcript = "well I dont see jack shit here at all honestly"

    assert clamp_title("I dont see jack shit", "Heuristic title", transcript) == "Heuristic title"


def test_a_title_that_merely_shares_words_is_kept() -> None:
    transcript = "he showed alexa the spoon and it described it"

    assert clamp_title("Alexa described a spoon with holes", "fb", transcript) != "fb"


def test_a_title_too_short_to_be_one_falls_back() -> None:
    assert clamp_title("tiny", "Heuristic title") == "Heuristic title"


def test_quote_detection_ignores_case_and_punctuation() -> None:
    assert is_quote("I don't SEE, jack shit!", "well i dont see jack shit here")


def test_hashtags_are_lowercased_deduplicated_and_stripped() -> None:
    tags = normalise_hashtags(["#Viral!", "Machine Learning", "viral", "a", "#AI"])

    assert tags == ("#viral", "#machinelearning", "#ai")


def test_hashtags_are_capped_at_what_youtube_reads() -> None:
    tags = normalise_hashtags([f"tag{index}" for index in range(40)])

    assert len(tags) == MAX_HASHTAGS


def test_hashtags_survive_a_model_returning_nonsense() -> None:
    assert normalise_hashtags(None) == ()
    assert normalise_hashtags("not a list") == ()


def test_keyword_tags_stay_inside_the_character_budget() -> None:
    post = PostMetadata("t", "d", tuple("#" + "x" * 60 for _ in range(12)))

    assert sum(len(tag) for tag in post.tags()) <= TAG_BUDGET


def test_the_description_credits_the_source_episode() -> None:
    description = compose_description(
        "What happens in the clip.", hashtags=("#ai",),
        source_title="Episode 12", source_url="https://youtu.be/abcdefghijk",
    )

    assert "Episode 12" in description
    assert "https://youtu.be/abcdefghijk" in description


def test_the_description_holds_without_a_source() -> None:
    assert compose_description("Body only.") == "Body only."


def test_an_overlong_description_is_truncated() -> None:
    description = compose_description("word " * 4000, source_title="Ep")

    assert len(description) <= DESCRIPTION_LIMIT
