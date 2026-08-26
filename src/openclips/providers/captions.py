"""Deterministic SRT and ASS subtitle generation from transcript words."""

from dataclasses import dataclass

from openclips.domain.captions import CaptionStyle
from openclips.domain.transcripts import TranscriptDocument, TranscriptWord

_MAX_LINE_WORDS = 6


@dataclass(frozen=True)
class CaptionEdit:
    """A transcript edit applied before caption rendering."""

    match: str
    replacement: str


def _clip_words(document: TranscriptDocument, start: float, end: float) -> list[TranscriptWord]:
    return [
        word
        for segment in document.segments
        if segment.end > start and segment.start < end
        for word in segment.words
        if word.end > start and word.start < end
    ]


def _apply_edits(
    words: list[TranscriptWord], edits: tuple[CaptionEdit, ...]
) -> list[TranscriptWord]:
    if not edits:
        return words
    edited: list[TranscriptWord] = []
    for word in words:
        text = word.text
        for edit in edits:
            if text == edit.match:
                text = edit.replacement
                break
        edited.append(
            TranscriptWord(text=text, start=word.start, end=word.end, probability=word.probability)
        )
    return edited


def _mask_profanity(
    words: list[TranscriptWord], mask_words: frozenset[str]
) -> list[TranscriptWord]:
    if not mask_words:
        return words
    masked: list[TranscriptWord] = []
    for word in words:
        text = word.text
        if text.lower() in mask_words:
            text = "*" * max(len(text), 1)
        masked.append(
            TranscriptWord(text=text, start=word.start, end=word.end, probability=word.probability)
        )
    return masked


def prepare_words(
    document: TranscriptDocument,
    clip_start: float,
    clip_end: float,
    *,
    edits: tuple[CaptionEdit, ...] = (),
    mask_words: frozenset[str] = frozenset(),
) -> list[TranscriptWord]:
    """Select, edit, and mask the words that appear inside a clip span."""
    words = _clip_words(document, clip_start, clip_end)
    words = _apply_edits(words, edits)
    return _mask_profanity(words, mask_words)


def _format_timestamp(seconds: float, comma: bool) -> str:
    total = max(seconds, 0.0)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = int(total % 60)
    fraction = int(round((total - int(total)) * 1000))
    separator = "," if comma else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{fraction:03d}"


def build_srt(words: list[TranscriptWord], *, uppercase: bool = False) -> str:
    """Render plain SRT cues with at most six words per line."""
    cues: list[str] = []
    for index in range(0, len(words), _MAX_LINE_WORDS):
        group = words[index : index + _MAX_LINE_WORDS]
        text = " ".join(word.text for word in group)
        if uppercase:
            text = text.upper()
        start = group[0].start
        end = group[-1].end or group[0].start + 0.5
        timing = f"{_format_timestamp(start, True)} --> {_format_timestamp(end, True)}"
        cues.append(f"{len(cues) + 1}\n{timing}\n{text}\n")
    return "\n".join(cues)


def _ass_time(seconds: float) -> str:
    total = max(seconds, 0.0)
    hundredths = int(round(total * 100))
    hours = hundredths // 360000
    minutes = (hundredths % 360000) // 6000
    secs = (hundredths % 6000) // 100
    rest = hundredths % 100
    return f"{hours}:{minutes:02d}:{secs:02d}.{rest:02d}"


def _karaoke_text(group: list[TranscriptWord], style: CaptionStyle) -> str:
    parts: list[str] = []
    for word in group:
        duration_cs = max(int(round((word.end - word.start) * 100)), 1)
        text = word.text.upper() if style.uppercase else word.text
        if style.word_highlight:
            parts.append(r"{\kf" + str(duration_cs) + "}" + text)
        else:
            parts.append(text)
    highlighted = " ".join(parts)
    if not style.word_highlight:
        return highlighted
    return r"{\rDefault}" + highlighted


def build_ass(
    words: list[TranscriptWord],
    style: CaptionStyle,
    *,
    width: int = 1080,
    height: int = 1920,
    clip_offset: float = 0.0,
) -> str:
    """Render an ASS subtitle file; karaoke templates highlight per word."""
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,{style.font_size},{style.primary_color},&H000000FF,"
        f"{style.outline_color},&H80000000,0,0,0,0,100,100,0,0,1,"
        f"{style.outline_width},1,2,60,60,220,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    events: list[str] = []
    for index in range(0, len(words), _MAX_LINE_WORDS):
        group = words[index : index + _MAX_LINE_WORDS]
        start = group[0].start - clip_offset
        end = max(group[-1].end - clip_offset, start + 0.5)
        text = _karaoke_text(group, style)
        events.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}"
        )
    return header + "\n".join(events) + "\n"
