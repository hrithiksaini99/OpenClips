# OpenClips

Cuts vertical short-form clips from long podcasts and posts them to YouTube on a
schedule. Runs natively on the machine — no Docker, no database, no broker.

`src/studio/` is the live pipeline. `src/openclips/` is an earlier
Docker/PostgreSQL/Redis service kept for reference; it is not what runs.

## Every post carries a copyright disclaimer

The clips are cut from other people's episodes. **Every description must contain
both the source credit and the copyright disclaimer, without exception.** Both
are assembled in `compose_description` in `src/studio/metadata.py`, and neither
depends on what the model returned, so no clip can be posted without them.

When editing that function, keep two properties:

- The disclaimer and the credit are appended by the code, never asked for in the
  prompt. A local model will drop them.
- The body is trimmed to fit around them, rather than the whole description
  being cut to length — otherwise a long description silently drops the two
  parts that must always appear.

The wording is `DISCLAIMER` in `src/studio/metadata.py`, overridable with
`OPENCLIPS_DISCLAIMER` so a channel can use its own text and contact address.

A disclaimer is a courtesy and a takedown route. It is not a legal defence and
does not create fair use — only how the clip is actually used decides that.

## Conventions

- Plain standard library over SDKs where the work is a few HTTP calls; the
  deployment target is a laptop and every dependency has to earn its place.
- State is JSON on disk, written through `pipeline.atomic_write_json` so a
  reader never sees a half-written file.
- Anything that talks to a model or a network degrades to a working default
  rather than failing the run.
- Tests for the live pipeline live in `tests/native/`. Run `pytest -q`,
  `ruff check src tests` and `mypy src` before pushing; CI runs all three.
