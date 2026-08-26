"""Durable FIFO job queues used to coordinate work between API and worker."""

import threading
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis import Redis


class InMemoryJobQueue:
    """Thread-safe in-process queue with the same surface as RedisJobQueue."""

    def __init__(self) -> None:
        self._items: deque[str] = deque()
        self._lock = threading.Lock()

    def enqueue(self, queue_name: str, payload: str) -> None:
        with self._lock:
            self._items.append(payload)

    def claim(self, queue_name: str, timeout_seconds: float = 0.0) -> str | None:
        del timeout_seconds
        with self._lock:
            if self._items:
                return self._items.popleft()
            return None

    def depth(self, queue_name: str) -> int:
        del queue_name
        with self._lock:
            return len(self._items)


class RedisJobQueue:
    """Redis list-backed FIFO queue; claims block briefly instead of busy-polling."""

    def __init__(self, redis_client: "Redis") -> None:
        self._redis = redis_client

    def enqueue(self, queue_name: str, payload: str) -> None:
        self._redis.rpush(queue_name, payload)

    def claim(self, queue_name: str, timeout_seconds: float = 5.0) -> str | None:
        result = self._redis.blpop(queue_name, timeout=timeout_seconds)
        if result is None:
            return None
        _name, payload = result
        return payload.decode() if isinstance(payload, bytes) else str(payload)
