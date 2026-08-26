# OpenClips Media Ingestion Design

## Goal

Register and safely materialize local MP4/MOV uploads and individual YouTube video URLs as durable source assets without duplicate files or duplicate jobs.

## Scope

Phase 1 supports local upload streams and individual YouTube video URLs. YouTube channel polling remains a later ingestion extension because reliable channel ownership, pagination, and polling checkpoints require a separate acceptance gate. This phase records the source type and external identifier needed by that extension.

## Domain contract

`SourceAsset` has a UUID, source kind (`LOCAL_UPLOAD` or `YOUTUBE_VIDEO`), original locator, optional external ID, stable SHA-256 idempotency key, sanitized display filename, media path, byte size, lifecycle status, seven-day source retention deadline, and timestamps.

Source status transitions are:

```text
PENDING -> INGESTING -> READY
                    -> FAILED -> PENDING
```

An already registered idempotency key returns the existing source and must not create a second file or ingestion job.

## Provider contracts

`MediaStorage` atomically writes a binary stream to a key under the configured media root and returns immutable metadata. It rejects traversal, absolute destinations, symlinks that escape the root, and partial-file visibility.

`YouTubeDownloader` accepts a validated canonical watch URL and destination directory, invokes yt-dlp without a shell, reports progress through a callback, and returns one downloaded media path plus a stable video ID. Tests use a fake process runner; the verification gate includes a harmless metadata-only live URL check when network access permits.

`LocalUploadIngestor` accepts filename and binary stream, permits `.mp4` and `.mov` case-insensitively, computes SHA-256 while writing, and returns a registered ready source.

## Recovery

Ingestion orchestration creates or reuses a source record before materialization. A retry of `FAILED` moves the source back to `PENDING`; a retry of an existing `READY` asset returns it unchanged. Failures preserve an actionable message and never expose a partial final file.

## Verification

Unit tests cover URL validation, filename safety, state transitions, duplicate handling, progress parsing, and partial-write cleanup. PostgreSQL tests cover source persistence and idempotency uniqueness. FFmpeg/FFprobe generates and inspects a tiny local fixture. The full Compose suite, migration check, Ruff, and mypy must pass before Phase 1 is verified.
