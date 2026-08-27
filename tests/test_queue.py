from openclips.infrastructure.queue import InMemoryJobQueue, QueueReceipt


def test_enqueue_claim_is_fifo() -> None:
    queue = InMemoryJobQueue()

    queue.enqueue("default", "first")
    queue.enqueue("default", "second")

    assert queue.claim("default") == QueueReceipt("default", "first")
    assert queue.claim("default") == QueueReceipt("default", "second")


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


def test_claim_moves_message_until_ack() -> None:
    queue = InMemoryJobQueue()
    queue.enqueue("default", "job-1")

    receipt = queue.claim("default", timeout_seconds=0)

    assert receipt == QueueReceipt("default", "job-1")
    assert queue.depth("default") == 0
    assert queue.processing_depth("default") == 1
    queue.ack(receipt)
    assert queue.processing_depth("default") == 0


def test_restore_processing_redelivers_unacked_message() -> None:
    queue = InMemoryJobQueue()
    queue.enqueue("default", "job-1")
    queue.claim("default", timeout_seconds=0)

    assert queue.restore_processing("default") == 1
    receipt = queue.claim("default", timeout_seconds=0)
    assert receipt is not None
    assert receipt.payload == "job-1"
