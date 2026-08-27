from enum import StrEnum


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"


def outbox_backoff_seconds(attempts: int, cap_seconds: int) -> int:
    if attempts < 1:
        raise ValueError("Outbox backoff requires at least one attempt")
    return int(min(2 ** (attempts - 1), cap_seconds))
