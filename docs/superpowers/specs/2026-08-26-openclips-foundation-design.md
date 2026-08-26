# OpenClips Foundation Design

## Goal

Build a small, production-oriented foundation that safely supports a resumable long-form video processing pipeline.

## Decisions

- Python/FastAPI backend with typed domain objects.
- PostgreSQL is the durable state store; Redis is the queue dependency.
- A modular monolith is preferred for V1 deployment simplicity.
- Domain transitions are pure and tested independently from persistence and HTTP.
- Filesystem media storage is represented by paths and metadata; upload/download behavior belongs to Phase 1.
- Authentication is reserved as an API boundary for Phase 0 and implemented in Phase 5.

## Phase 0 interfaces

- `JobStatus` and `ClipStatus` enums define allowed states.
- `JobStateMachine.transition(current, event)` returns the next state or raises a domain error.
- `GET /health` reports process health.
- `GET /ready` reports configured dependency readiness.
- Repository protocols isolate persistence from services.

## Error handling

Invalid transitions are client-visible validation errors. Infrastructure failures are recorded on jobs with an attempt count and retryable flag. No background operation may silently discard work.

## Testing

Unit tests cover every allowed and rejected state transition. API tests cover health and readiness responses. Persistence tests use a disposable PostgreSQL service when available; a fast repository fake keeps domain tests independent. Compose smoke testing is part of the phase gate.
