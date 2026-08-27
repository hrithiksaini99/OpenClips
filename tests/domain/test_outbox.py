import pytest

from openclips.domain.outbox import outbox_backoff_seconds


def test_outbox_backoff_is_exponential_and_capped() -> None:
    assert outbox_backoff_seconds(1, 300) == 1
    assert outbox_backoff_seconds(2, 300) == 2
    assert outbox_backoff_seconds(20, 300) == 300


def test_outbox_backoff_rejects_nonpositive_attempts() -> None:
    with pytest.raises(ValueError, match="Outbox backoff requires at least one attempt"):
        outbox_backoff_seconds(0, 300)
