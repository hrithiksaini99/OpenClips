"""Fetching the source, and explaining it when YouTube says no.

yt-dlp answers a refusal with a dozen lines of warnings and one actionable
sentence somewhere in the middle. These cover the translation, and the rule that
a rate-limited download is not retried — trying again immediately is what earned
the limit.
"""

from __future__ import annotations

import pytest

from studio import pipeline


def test_a_javascript_runtime_is_found_when_one_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # YouTube extraction without one is deprecated, and yt-dlp enables only Deno
    # by default, so whatever is actually installed has to be passed explicitly.
    pipeline.js_runtime.cache_clear()
    monkeypatch.setattr(
        pipeline.shutil, "which", lambda name: "/bin/node" if name == "node" else None
    )

    assert pipeline.js_runtime() == "node"
    assert pipeline._yt_dlp_flags()[-2:] == ["--js-runtimes", "node"]

    pipeline.js_runtime.cache_clear()


def test_no_runtime_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline.js_runtime.cache_clear()
    monkeypatch.setattr(pipeline.shutil, "which", lambda _name: None)

    assert pipeline.js_runtime() == ""

    pipeline.js_runtime.cache_clear()


def test_cookies_are_only_read_when_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cookies are the user's live YouTube session, so nothing is read by default.
    monkeypatch.delenv("OPENCLIPS_COOKIES_FROM_BROWSER", raising=False)
    monkeypatch.delenv("OPENCLIPS_COOKIES_FILE", raising=False)

    assert pipeline._auth_flags() == []


def test_a_named_browser_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCLIPS_COOKIES_FROM_BROWSER", "chrome")

    assert pipeline._auth_flags() == ["--cookies-from-browser", "chrome"]


def test_an_exported_cookie_jar_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCLIPS_COOKIES_FROM_BROWSER", raising=False)
    monkeypatch.setenv("OPENCLIPS_COOKIES_FILE", "/tmp/cookies.txt")

    assert pipeline._auth_flags() == ["--cookies", "/tmp/cookies.txt"]


def test_rate_limiting_is_explained_rather_than_dumped() -> None:
    output = "WARNING: [youtube] Unable to download webpage: HTTP Error 429: Too Many Requests"

    assert "rate-limiting this machine" in pipeline.diagnose_download(output)
    assert pipeline.is_rate_limited(output)


def test_the_bot_check_names_the_setting_that_fixes_it() -> None:
    output = "ERROR: [youtube] Sign in to confirm you\u2019re not a bot. Use --cookies-from-browser"

    assert "OPENCLIPS_COOKIES_FROM_BROWSER" in pipeline.diagnose_download(output)
    assert pipeline.is_rate_limited(output)


def test_a_missing_runtime_is_explained() -> None:
    output = "WARNING: [youtube] No supported JavaScript runtime could be found."

    assert "JavaScript runtime" in pipeline.diagnose_download(output)
    assert not pipeline.is_rate_limited(output)


def test_an_unavailable_video_is_not_mistaken_for_a_rate_limit() -> None:
    output = "ERROR: [youtube] abc: Video unavailable"

    assert "unavailable" in pipeline.diagnose_download(output)
    assert not pipeline.is_rate_limited(output)


def test_an_unrecognised_failure_gets_no_invented_explanation() -> None:
    assert pipeline.diagnose_download("ERROR: something entirely new") == ""
    assert not pipeline.is_rate_limited("ERROR: something entirely new")
