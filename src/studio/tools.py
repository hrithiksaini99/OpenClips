"""Locate external binaries without depending on the caller's PATH.

The server is normally started by invoking the virtualenv's interpreter directly
(`.venv/bin/python -m studio.server`) rather than by activating the environment,
so the venv's `bin/` is not on PATH and console scripts such as `yt-dlp` are
invisible to a bare subprocess call. Homebrew's prefix is likewise missing from
some launch contexts (GUI launchers, LaunchAgents).
"""

from __future__ import annotations

import shutil
import sys
from functools import cache
from pathlib import Path

# Checked before PATH so a project-local install always wins.
_EXTRA_DIRS = (
    Path(sys.executable).parent,  # the active virtualenv's bin/
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path("/usr/bin"),
)

_HINTS = {
    "yt-dlp": "pip install yt-dlp (inside this project's virtualenv)",
    "ffmpeg": "brew install ffmpeg",
    "ffprobe": "brew install ffmpeg",
}


class MissingToolError(RuntimeError):
    """Raised when a required external binary cannot be located."""


@cache
def binary(name: str) -> str:
    """Return an absolute path to `name`, raising an actionable error if absent."""
    for directory in _EXTRA_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    hint = _HINTS.get(name, f"install {name}")
    raise MissingToolError(f"Required tool '{name}' was not found. Install it with: {hint}")
