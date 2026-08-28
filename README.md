# OpenClips

Turn a long podcast or interview into vertical short-form clips, entirely on your
own machine. Give it a YouTube link or a local file and it returns 9:16 MP4s with
word-by-word captions burned in, framed on whoever is speaking, ready to post.

No cloud service, no per-clip credits, no watermarks. Transcription and clip
selection both run locally.

## What it produces

Each clip is a 1080x1920 MP4 containing:

- **Word-by-word captions**, the active word highlighted, drawn directly by the
  renderer (no libass or system fonts required beyond a bold TTF)
- **Face-aware framing** that follows the speaker and re-frames at camera cuts,
  instead of a fixed centre crop that slices people out of shot
- **A complete thought**, 28-75 seconds, starting on a real sentence and ending
  on terminal punctuation
- **Normalised audio** at roughly -14 LUFS, the level social platforms target
- A title taken from the moment's own hook

## Requirements

| | |
|---|---|
| Python | 3.12 |
| FFmpeg | any recent build, on `PATH` (`brew install ffmpeg`, `winget install ffmpeg`, `apt install ffmpeg`) |
| Disk | ~3 GB per hour of source video while a job runs |
| RAM | 8 GB works; the pipeline sizes itself to what is free |
| Ollama | optional, only for AI clip ranking |

FFmpeg does **not** need `libass` or `freetype`: captions are rendered by the
application and composited as images, so minimal FFmpeg builds work fine.

## Install

```bash
git clone https://github.com/hrithiksaini99/OpenClips.git
cd OpenClips
python3.12 -m venv .venv-native
.venv-native/bin/pip install -r requirements-studio.txt
```

On Windows use `.venv-native\Scripts\pip` instead.

## Run

```bash
PYTHONPATH=src .venv-native/bin/python -m studio.server
```

Then open **http://127.0.0.1:8080**.

Paste a link, choose how many clips you want, and press **Generate clips**.
Progress is live; finished clips appear as playable cards you can download.

Accepted inputs:

```
https://www.youtube.com/watch?v=VIDEO_ID
youtube.com/watch?v=VIDEO_ID          # scheme optional
https://youtu.be/VIDEO_ID
https://www.youtube.com/@channel/videos   # uses the channel's latest episode
/path/to/local/episode.mp4
```

Clips are written to `clips/<job-id>/` inside the project, alongside a
`job.json` describing each one. Downloaded sources are cached in `media/source/`
and reused, so re-running an episode costs nothing.

## Controls

| Control | Meaning |
|---|---|
| **Clips** | How many to produce |
| **Model** | Whisper size. `small` is the sweet spot; `medium` is more accurate and roughly twice as slow |
| **Parallel** | Requested concurrency. The pipeline lowers this if memory is tight |
| **Pick clips** | `AI ranked` uses a local LLM; `Fast` uses heuristics only |

## AI clip ranking (optional)

With [Ollama](https://ollama.com) running, a local model reads the shortlisted
moments, scores them, and writes each title. Without it, the heuristic ranking is
used and nothing fails.

```bash
ollama pull gemma3:4b
export OPENCLIPS_LLM_MODEL=gemma3:4b     # default: gemma4:latest
export OPENCLIPS_OLLAMA_HOST=http://localhost:11434
```

Pick a model that fits your VRAM. On a 4 GB card a ~4B model is appropriate; a
larger one still works but runs on the CPU, adding a minute or two per episode.

## How it works

```
URL ─┬─> audio  ──> transcribe ──> sentences ──> shortlist ──> LLM rank ─┐
     └─> video  ─────────────────────────────────────────────────────────┴─> render
```

1. **Download** — audio and video are fetched concurrently as separate streams
   and never merged. Audio is small, so transcription starts while the video is
   still arriving.
2. **Transcribe** — faster-whisper with word-level timestamps, run over parallel
   slices sharing one model instance.
3. **Select** — words are grouped into sentences; whole-sentence spans are scored
   on hook strength, substance, speech density and length.
4. **Rank** — an optional local LLM picks the final set and titles them.
5. **Render** — per clip, in parallel: face-tracked crop, caption overlay,
   loudness normalisation.

## Performance

Measured on an M4 Pro for a 2.5 hour episode:

| Stage | Time |
|---|---|
| Download | ~12 min (concurrent fragments; ~2-3 MB/s) |
| Transcription | ~10 min at `small` |
| Selection + LLM ranking | under a minute |
| Rendering 12 clips | ~1-2 min |

Transcription peaks around 1.4 GB regardless of worker count, because parallel
slices share a single model rather than loading one copy each.

### GPU

Transcription runs on the CPU by default. On an NVIDIA card, CTranslate2 supports
CUDA and `large-v3` at int8 needs roughly 2.5 GB of VRAM. On Apple Silicon
CTranslate2 has no Metal backend, so the CPU path is used there.

## Troubleshooting

**`Required tool 'yt-dlp' was not found`** — install it into the same virtualenv
you run the server from: `.venv-native/bin/pip install yt-dlp`.

**A download fails** — yt-dlp is upgraded and the download retried once
automatically, since YouTube-side changes are the usual cause. If it still fails,
upgrade manually: `.venv-native/bin/pip install -U yt-dlp`.

**An interrupted download** resumes from where it stopped on the next run; do not
delete `media/source/` if you want to keep that progress.

**Captions look wrong or missing** — the renderer needs a bold TrueType font. It
looks for Arial Black, Impact, then DejaVu Sans Bold, and falls back to a default
face if none is present.

## Repository layout

```
src/studio/      the native pipeline described above
src/openclips/   an earlier Docker/PostgreSQL/Redis service with a review
                 dashboard and publishing queues; independent of src/studio
web/index.html   the browser UI
clips/           generated clips (git-ignored)
media/           downloaded sources, cached by video id (git-ignored)
```

## Not implemented

Honest about the gaps: no split-screen or multi-speaker layouts, no automatic
channel polling (a channel URL takes the latest episode only), no direct posting
to social platforms from the studio pipeline, and no B-roll or zoom effects.

## Licence

Undecided.
