"""Durable FIFO job queues used to coordinate work between API and worker."""

import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis import Redis


@dataclass(frozen=True)
class QueueReceipt:
    """A claimed payload retained in a queue's processing list until acknowledged."""

    queue_name: str
    payload: str


class InMemoryJobQueue:
    """Thread-safe in-process queue with the same surface as RedisJobQueue."""

    def __init__(self) -> None:
        self._ready: defaultdict[str, deque[str]] = defaultdict(deque)
        self._processing: defaultdict[str, deque[str]] = defaultdict(deque)
        self._lock = threading.Lock()

    def enqueue(self, queue_name: str, payload: str) -> None:
        with self._lock:
            self._ready[queue_name].append(payload)

    def claim(self, queue_name: str, timeout_seconds: float = 0.0) -> QueueReceipt | None:
        del timeout_seconds
        with self._lock:
            if self._ready[queue_name]:
                payload = self._ready[queue_name].popleft()
                self._processing[queue_name].append(payload)
                return QueueReceipt(queue_name, payload)
            return None

    def ack(self, receipt: QueueReceipt) -> None:
        with self._lock:
            self._processing[receipt.queue_name].remove(receipt.payload)

    def restore_processing(self, queue_name: str) -> int:
        with self._lock:
            restored = len(self._processing[queue_name])
            while self._processing[queue_name]:
                self._ready[queue_name].appendleft(self._processing[queue_name].pop())
            return restored

    def depth(self, queue_name: str) -> int:
        with self._lock:
            return len(self._ready[queue_name])

    def processing_depth(self, queue_name: str) -> int:
        with self._lock:
            return len(self._processing[queue_name])


class RedisJobQueue:
    """Redis list-backed FIFO queue; claims block briefly instead of busy-polling."""

    def __init__(self, redis_client: "Redis") -> None:
        self._redis = redis_client

    def enqueue(self, queue_name: str, payload: str) -> None:
        self._redis.rpush(self._ready_key(queue_name), payload)

    def claim(self, queue_name: str, timeout_seconds: float = 5.0) -> QueueReceipt | None:
        payload = self._redis.blmove(
            self._ready_key(queue_name),
            self._processing_key(queue_name),
            timeout=int(timeout_seconds),
            src="LEFT",
            dest="RIGHT",
        )
        if payload is None:
            return None
        return QueueReceipt(queue_name, self._payload_text(payload))

    def ack(self, receipt: QueueReceipt) -> None:
        self._redis.lrem(self._processing_key(receipt.queue_name), 1, receipt.payload)

    def restore_processing(self, queue_name: str) -> int:
        restored = 0
        while self._redis.lmove(
            self._processing_key(queue_name), self._ready_key(queue_name), "RIGHT", "LEFT"
        ) is not None:
            restored += 1
        return restored

    def depth(self, queue_name: str) -> int:
        return int(self._redis.llen(self._ready_key(queue_name)))

    def processing_depth(self, queue_name: str) -> int:
        return int(self._redis.llen(self._processing_key(queue_name)))

    @staticmethod
    def _ready_key(queue_name: str) -> str:
        return f"openclips:{queue_name}:ready"

    @staticmethod
    def _processing_key(queue_name: str) -> str:
        return f"openclips:{queue_name}:processing"

    @staticmethod
    def _payload_text(payload: bytes | str) -> str:
        return payload.decode() if isinstance(payload, bytes) else str(payload)
