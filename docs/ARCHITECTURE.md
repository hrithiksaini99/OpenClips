# OpenClips architecture

OpenClips is a modular monolith for V1: one API process, one worker process, PostgreSQL for durable state, Redis for queue coordination, and a user-visible filesystem media root. This keeps deployment simple while preserving boundaries for later extraction.

```text
Web/API client -> FastAPI API -> PostgreSQL
                         |      Redis -> Worker
                         |                  |
                         +------------ providers/media/local AI
```

The domain layer owns lifecycle rules. Application services translate commands into domain operations and durable jobs. Infrastructure adapters implement database, queue, filesystem, FFmpeg, YouTube, social, transcription, and local-model details. HTTP handlers must not contain provider logic or direct lifecycle mutation.

The first implementation uses interfaces and in-memory fakes where an external runtime would make tests slow or unavailable. Real integrations are added behind those contracts in later phases.
