import importlib
import importlib.util
from threading import Event, Thread

import pytest


def _stage_limiter_type():  # type: ignore[no-untyped-def]
    module_name = "openclips.application.concurrency"
    assert importlib.util.find_spec(module_name) is not None, "StageLimiter module is missing"
    return importlib.import_module(module_name).StageLimiter


def test_named_stage_limit_blocks_until_the_active_context_exits() -> None:
    limiter = _stage_limiter_type()({"transcribe": 1})
    first_entered = Event()
    release_first = Event()
    second_entered = Event()

    def hold_first_permit() -> None:
        with limiter.limit("transcribe"):
            first_entered.set()
            release_first.wait(timeout=2)

    def enter_second() -> None:
        with limiter.limit("transcribe"):
            second_entered.set()

    first = Thread(target=hold_first_permit)
    second = Thread(target=enter_second)
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()

    second.join(timeout=0.05)
    try:
        assert second.is_alive()
        assert not second_entered.is_set()
    finally:
        release_first.set()
        first.join(timeout=1)
        second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()


def test_unknown_stage_never_blocks_behind_a_limited_stage() -> None:
    limiter = _stage_limiter_type()({"transcribe": 1})
    entered = Event()

    with limiter.limit("transcribe"):
        thread = Thread(target=lambda: _enter_stage(limiter, "unlimited", entered))
        thread.start()
        thread.join(timeout=0.2)

    assert not thread.is_alive()
    assert entered.is_set()


def test_raising_body_releases_the_stage_permit() -> None:
    limiter = _stage_limiter_type()({"transcribe": 1})

    with pytest.raises(RuntimeError, match="transcription failed"), limiter.limit("transcribe"):
        raise RuntimeError("transcription failed")

    entered = Event()
    thread = Thread(target=lambda: _enter_stage(limiter, "transcribe", entered))
    thread.start()
    thread.join(timeout=0.2)

    assert not thread.is_alive()
    assert entered.is_set()


def _enter_stage(limiter: object, stage: str, entered: Event) -> None:
    with limiter.limit(stage):  # type: ignore[attr-defined]
        entered.set()
