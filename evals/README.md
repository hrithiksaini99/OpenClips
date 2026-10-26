# Clip-selection evals

The selector in `src/studio/select.py` decides what the product outputs, and its
scoring is a pile of tunable weights. Changing one weight is a code change that
no unit test currently treats as a behavioural change: the existing tests assert
*how* the score is computed, not *which clips come out*.

This directory closes that gap. It runs the real `load_sentences` → `find_clips`
path over fixed transcripts and asserts on the clips that come back, then fails
CI when a case that passed in the committed baseline starts failing.

```console
$ PYTHONPATH=src:../agent-eval/src python -m agenteval run \
    --suite evals/suite.jsonl \
    --agent evals/adapter.py:run \
    --baseline evals/baseline.json \
    --report evals/report.md \
    --fail-on-regression --fail-under 1.0

8/8 passed (100.0%)
```

## What is covered

| Area | Cases | What would break it |
| --- | --- | --- |
| Hook detection | `hook-heavy-selects-both-hooks` | A scoring change that stops surfacing the strongest openers |
| Score floor | `hook-heavy-scores-above-floor` | Weights drifting down until any span looks acceptable |
| Duration bounds | `hook-heavy-structure`, `hook-heavy-respects-duration-bounds` | An off-by-one in the 28–75s window |
| Non-overlap | `hook-heavy-structure`, `filler-heavy-structure` | Greedy selection losing its `min_gap_seconds` check |
| Complete thoughts | `hook-heavy-selects-both-hooks` | Windows ending mid-sentence |
| Parameters | `hook-heavy-respects-max-clips`, `…-duration-bounds` | `max_clips` / `max_seconds` no longer being plumbed through |
| Degenerate input | `short-episode-yields-nothing` | A 14-second episode producing a 28-second clip |

## What the first run found

Two things the unit tests could not see:

1. **There is no minimum score.** `find_clips` returns the best windows regardless of
   absolute quality, so a 100-second episode of pure hedging still produces two "clips"
   (scores 4.1 and 6.6, against 10.8 and 11.3 for the real episode). The case
   `filler-heavy-yields-nothing` is marked `"optional": true` so the gap stays visible in
   every report without blocking merges until it is fixed. A `min_score` parameter is the
   obvious fix.
2. **Results are ordered by start time, not by score.** `find_clips` picks by score but
   returns in playback order, so the first clip is not the best clip. That is the right
   product behaviour — shortlists should read in episode order — but it is easy to assume
   otherwise when reading the code, and the `clips_ordered` grader now pins it.

## Layout

```
evals/
  adapter.py        # the only file that knows both the harness and the selector
  build_fixtures.py # regenerates the fixtures from readable prose
  fixtures/*.json   # Whisper-shaped transcripts, committed
  suite.jsonl       # one case per line
  baseline.json     # the committed reference run
```

## CI

`.github/workflows/eval-gate.yml` runs the suite on every push and PR against the
committed baseline. `agenteval` is not on PyPI yet, so the install step is
currently guarded: if it cannot be fetched, the job emits a warning instead of
failing every build. Remove the `continue-on-error` guard (and pin the install to
a tag) once <https://github.com/hrithiksaini99/agent-eval> is public.

## Updating the baseline

Only when behaviour changes on purpose, and say why in the PR:

```bash
PYTHONPATH=src:../agent-eval/src python -m agenteval run \
  --suite evals/suite.jsonl --agent evals/adapter.py:run \
  --out evals/baseline.json --report /tmp/report.md
```

A baseline bump that is not explained in the commit message is how an eval suite
becomes decoration.

## Adding a fixture

Edit the prose in `build_fixtures.py`, run it, add cases, then regenerate the
baseline. The fixtures are committed so a suite run never depends on the script.
