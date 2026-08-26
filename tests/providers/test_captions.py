"""Deterministic caption generation tests: SRT, ASS karaoke, edits, masking."""

import pytest

from openclips.domain.captions import (
    CAPTION_TEMPLATES,
    CaptionStyle,
    UnknownCaptionTemplateError,
    get_template,
)
from openclips.domain.transcripts import TranscriptDocument, TranscriptSegment, TranscriptWord
from openclips.providers.captions import CaptionEdit, build_ass, build_srt, prepare_words


def _word(text: str, start: float, end: float) -> TranscriptWord:
    return TranscriptWord(text=text, start=start, end=end, probability=0.95)


@pytest.fixture
def words() -> list[TranscriptWord]:
    return [
        _word("This", 0.0, 0.4),
        _word("damn", 0.4, 0.8),
        _word("idea", 0.8, 1.3),
        _word("works", 1.3, 1.9),
        _word("always", 2.9, 3.5),
        _word("trust", 3.5, 4.1),
        _word("me", 4.1, 4.4),
        _word("today", 4.4, 5.0),
    ]


def _document(words: list[TranscriptWord]) -> TranscriptDocument:
    segment = TranscriptSegment(
        start=words[0].start,
        end=words[-1].end,
        text=" ".join(w.text for w in words),
        words=tuple(words),
    )
    return TranscriptDocument(language="en", duration=words[-1].end, segments=(segment,))


def test_all_builtin_templates_validate() -> None:
    for name in CAPTION_TEMPLATES:
        style = get_template(name)

        assert style.name == name


def test_unknown_template_is_rejected() -> None:
    with pytest.raises(UnknownCaptionTemplateError, match="neon"):
        get_template("neon")


def test_invalid_style_parameters_are_rejected() -> None:
    with pytest.raises(ValueError, match="Font size"):
        CaptionStyle(
            name="x",
            font_size=4,
            primary_color="&H00FFFFFF",
            outline_color="&H00000000",
            outline_width=1,
            word_highlight=False,
            uppercase=False,
        )
    with pytest.raises(ValueError, match="Color"):
        CaptionStyle(
            name="x",
            font_size=40,
            primary_color="#fff",
            outline_color="&H00000000",
            outline_width=1,
            word_highlight=False,
            uppercase=False,
        )


def test_srt_cues_group_six_words_with_timestamps(words: list[TranscriptWord]) -> None:
    srt = build_srt(words)

    cues = srt.strip().split("\n\n")
    assert len(cues) == 2
    assert cues[0].startswith("1\n")
    assert "00:00:00,000 --> 00:00:04,100" in cues[0]
    assert "This damn idea works always trust" in cues[0]
    assert cues[1].startswith("2\n")
    assert "me today" in cues[1]


def test_srt_uppercase_matches_bold_style(words: list[TranscriptWord]) -> None:
    srt = build_srt(words, uppercase=True)

    assert "THIS DAMN IDEA WORKS ALWAYS TRUST" in srt


def test_prepare_words_applies_edits(words: list[TranscriptWord]) -> None:
    edited = prepare_words(_document(words), 0.0, 10.0, edits=(CaptionEdit("damn", "brilliant"),))

    assert [word.text for word in edited][:3] == ["This", "brilliant", "idea"]


def test_prepare_words_masks_profanity_only_when_configured(
    words: list[TranscriptWord],
) -> None:
    untouched = prepare_words(_document(words), 0.0, 10.0)
    masked = prepare_words(
        _document(words), 0.0, 10.0, mask_words=frozenset({"damn"})
    )

    assert untouched[1].text == "damn"
    assert masked[1].text == "****"


def test_karaoke_ass_contains_word_timings_and_highlight_tags(
    words: list[TranscriptWord],
) -> None:
    style = get_template("karaoke")
    ass = build_ass(words, style, clip_offset=0.0)

    assert "[Script Info]" in ass
    assert "PlayResX: 1080" in ass
    assert r"{\kf40}THIS" in ass
    assert "Dialogue: 0,0:00:00.00" in ass


def test_plain_ass_has_no_karaoke_tags(words: list[TranscriptWord]) -> None:
    style = get_template("minimal")

    ass = build_ass(words, style, clip_offset=0.0)

    assert "\\kf" not in ass


def test_ass_offsets_shift_cues_into_clip_timeline(words: list[TranscriptWord]) -> None:
    style = get_template("minimal")

    ass = build_ass(words, style, clip_offset=30.0)

    assert "Dialogue: 0,0:00:00.00," in ass.split("[Events]")[1]


def test_clip_window_selects_only_inner_words() -> None:
    document = _document(
        [
            _word("before", 0.0, 0.5),
            _word("inside", 5.0, 5.5),
            _word("after", 20.0, 20.5),
        ]
    )

    selected = prepare_words(document, 4.0, 6.0)

    assert [word.text for word in selected] == ["inside"]
