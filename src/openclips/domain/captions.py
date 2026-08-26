"""Built-in caption templates with validated styling parameters."""

from dataclasses import dataclass

CAPTION_TEMPLATES = (
    "minimal",
    "bold",
    "karaoke",
    "podcast",
    "high_contrast",
    "clean",
)
DEFAULT_CAPTION_TEMPLATE = "minimal"


class UnknownCaptionTemplateError(ValueError):
    """Raised when a caption template name is not one of the built-ins."""


@dataclass(frozen=True)
class CaptionStyle:
    """Validated visual parameters shared by every caption line."""

    name: str
    font_size: int
    primary_color: str
    outline_color: str
    outline_width: int
    word_highlight: bool
    uppercase: bool

    def __post_init__(self) -> None:
        if self.font_size < 8 or self.font_size > 200:
            msg = f"Font size {self.font_size} is outside the supported range"
            raise ValueError(msg)
        if self.outline_width < 0:
            msg = f"Outline width {self.outline_width} must not be negative"
            raise ValueError(msg)
        for color in (self.primary_color, self.outline_color):
            if not _is_ass_color(color):
                msg = f"Color {color!r} is not an &HAABBGGRR ASS color"
                raise ValueError(msg)


def _is_ass_color(value: str) -> bool:
    return value.startswith("&H") and len(value) == 10 and all(
        character in "0123456789ABCDEFabcdef" for character in value[2:]
    )


def get_template(name: str) -> CaptionStyle:
    """Return the built-in style for a template name."""
    styles = {
        "minimal": CaptionStyle(
            name="minimal",
            font_size=54,
            primary_color="&H00FFFFFF",
            outline_color="&H00000000",
            outline_width=2,
            word_highlight=False,
            uppercase=False,
        ),
        "bold": CaptionStyle(
            name="bold",
            font_size=72,
            primary_color="&H00FFFFFF",
            outline_color="&H00000000",
            outline_width=4,
            word_highlight=False,
            uppercase=True,
        ),
        "karaoke": CaptionStyle(
            name="karaoke",
            font_size=64,
            primary_color="&H0000E5FF",
            outline_color="&H00000000",
            outline_width=3,
            word_highlight=True,
            uppercase=True,
        ),
        "podcast": CaptionStyle(
            name="podcast",
            font_size=48,
            primary_color="&H00F0F0F0",
            outline_color="&H00201010",
            outline_width=2,
            word_highlight=False,
            uppercase=False,
        ),
        "high_contrast": CaptionStyle(
            name="high_contrast",
            font_size=60,
            primary_color="&H00FFFF00",
            outline_color="&H00000000",
            outline_width=5,
            word_highlight=False,
            uppercase=True,
        ),
        "clean": CaptionStyle(
            name="clean",
            font_size=52,
            primary_color="&H00FFFFFF",
            outline_color="&H00000000",
            outline_width=1,
            word_highlight=False,
            uppercase=False,
        ),
    }
    try:
        return styles[name]
    except KeyError as error:
        msg = f"Unknown caption template {name!r}; built-ins are {', '.join(CAPTION_TEMPLATES)}"
        raise UnknownCaptionTemplateError(msg) from error


def validate_template(name: str) -> CaptionStyle:
    """Alias of get_template kept explicit for configuration validation."""
    return get_template(name)
