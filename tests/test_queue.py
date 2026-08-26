from openclips.infrastructure.queue import InMemoryJobQueue


def test_enqueue_claim_is_fifo() -> None:
    queue = InMemoryJobQueue()

    queue.enqueue("default", "first")
    queue.enqueue("default", "second")

    assert queue.claim("default") == "first"
    assert queue.claim("default") == "second"


def test_empty_queue_claims_none() -> None:
    queue = InMemoryJobQueue()

    assert queue.claim("default", timeout_seconds=0.0) is None
    assert queue.depth("default") == 0


def test_depth_reflects_pending_payloads() -> None:
    queue = InMemoryJobQueue()

    queue.enqueue("default", "a")
    queue.enqueue("default", "b")

    assert queue.depth("default") == 2
    queue.claim("default")
    assert queue.depth("default") == 1
