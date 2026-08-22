# SpeakMD

SpeakMD is a local web application that converts large Markdown documents into
speech. It preserves document structure, narrates tables, saves each rendered
chunk as a durable audio checkpoint, and resumes after a pause, restart, or
failed chunk.

Everything runs on the local machine. There are no accounts, hosted services, or
external databases.

## Features

- Drag-and-drop Markdown upload in a lightweight local web interface.
- Speech-friendly handling of headings, prose, lists, quotations, links, inline
  code, fenced code, images, HTML text, task lists, deleted text, tables, and
  Unicode.
- Header-aware table narration. Every row repeats its column names, so it remains
  understandable as an independent audio checkpoint.
- Semantic chunks that prefer structure, sentences, and whitespace over arbitrary
  character splits.
- Per-chunk WAV checkpoints, atomic state updates, pause/resume, cancellation,
  retry, and restart recovery.
- Final lossless WAV and compact MP3 output.
- Live progress, queue state, CPU/RAM/GPU metrics, errors, warnings, and audio
  download links.

## Pipeline

```text
Markdown upload
  -> structured document parsing
  -> speech-friendly narration blocks
  -> semantic chunks
  -> persistent job state + independent WAV checkpoints
  -> local speech synthesis on CPU or CUDA
  -> final WAV assembly -> optional MP3 encode
```

One worker owns the loaded speech model and processes one document at a time.
This keeps memory use predictable, avoids GPU contention, and makes pause,
cancel, and recovery behavior straightforward.

## Markdown narration

- Headings become “Heading level N: …” and introduce the following content.
- Lists retain their item position; task-list state is spoken as completed or
  incomplete.
- Blockquotes begin with “Quotation”. Links preserve their visible text and a
  speech-friendly version of their destination.
- Inline and fenced code are never silently removed. Code punctuation is named
  and fenced lines are numbered.
- Images retain alt text and source; readable HTML text is preserved. Deleted
  text is marked as deleted instead of disappearing.
- A table starts with its column count, column names, and number of data rows.
  Each row is then narrated with headers, for example:

  > Table 1, row 2: Name: Sarah. Age: 28. Location: Paris.

Large tables are narrated row by row. Large cells and code blocks are split into
continuation chunks with their context repeated.

## Chunking and audio

The default chunk size is 360 characters. The planner preserves headings,
paragraphs, table rows, and code boundaries when possible; it then prefers full
sentences and whitespace. Only an unusually long unbroken token is split.

Each chunk has an appropriate trailing pause. Chunks are stored as mono 24 kHz
WAV, so a final WAV is assembled by streaming frames rather than loading a whole
document into memory. The optional MP3 is encoded at 64 kb/s mono.

## Installation

Requirements:

- Python 3.11 or 3.12.
- `uv` for environment and dependency management.
- `ffmpeg` on `PATH` to create the final MP3. A final WAV is still generated if
  it is unavailable.
- An NVIDIA GPU with a working driver is optional for CUDA acceleration.

From the project directory:

```bash
uv python install 3.12
uv sync --extra dev
```

The first command is only needed when a supported Python version is not already
available. The second command creates `.venv` and installs the application and
test dependencies.

To check an optional NVIDIA GPU:

```bash
nvidia-smi
```

Choose **Auto** or **CUDA GPU only** when a working GPU is available. Otherwise,
choose **Auto** or **CPU only**; the application will continue to work, with
slower synthesis.

## Run and use

```bash
uv run speakmd
```

Open <http://127.0.0.1:8000>.

1. Drop a Markdown file onto the page.
2. Choose voice, speed, device, and chunk size.
3. Select **Start conversion**.
4. Monitor the active chunk, overall progress, estimated remaining time, queue,
   resources, and any warnings or errors.
5. Download or play the completed MP3/WAV from the page.

**Pause** finishes and saves the current chunk before stopping. **Resume** starts
at the first missing checkpoint. **Cancel** stops at a safe chunk boundary while
keeping completed chunks. **Retry failed** reruns only failed work, or retries
final assembly without re-synthesizing valid audio.

The server listens on loopback by default. Set `SPEAKMD_PORT=8010` to use another
port and `SPEAKMD_OUTPUT=/absolute/path` to change the output location. Set
`SPEAKMD_HOST=0.0.0.0` only when deliberately exposing the application to a
trusted local network.

## Outputs and recovery

Every document/configuration pair has a stable directory under `output/`:

```text
output/
  report-<source-hash>-<settings-hash>/
    input.md
    job.json
    chunks/
      0001.wav
      0002.wav
    final/
      report.wav
      report.mp3
```

`job.json` records settings, narration chunks, per-chunk status, attempts,
timing, errors, and output paths. Both state and WAV files are written atomically.
On startup, interrupted jobs are recovered, and valid checkpoints are detected
before any synthesis begins. A sudden interruption loses at most the in-flight
chunk.

Uploading the same document with the same settings reopens its existing job, so
completed audio is not regenerated unnecessarily.

## Project structure

```text
src/speakmd/
  app.py               local web routes and static-file hosting
  static/index.html    dependency-free drag-and-drop interface
  markdown_speech.py   Markdown-to-narration conversion and table handling
  chunking.py          semantic chunk planner
  tts.py               lazy CPU/CUDA speech renderer
  jobs.py              persistent queue and recovery state machine
  audio.py             atomic WAV checkpoints and final assembly
  monitor.py           system resource snapshots
tests/test_pipeline.py Markdown and durable-pipeline regression tests
```

## Tests

```bash
uv run pytest
```

The test suite covers Markdown narration, tables, large table cells, Unicode,
chunk limits, final WAV/MP3 construction, pause/resume, restart recovery, and
checkpoint reuse. It uses a deterministic local test renderer, so it does not
require speech-model assets.

## Performance guidance

- Start with **Auto** device selection. It uses CUDA only when the runtime can
  access a working GPU.
- Keep the default single-worker policy. It avoids duplicate model loads and
  excessive GPU memory pressure.
- Start with 360-character chunks. Use 300 for code- or URL-heavy documents, or
  420 after checking that narration quality remains acceptable.
- Keep `output/` on a local SSD. WAV checkpoints trade disk space for reliable
  recovery and inexpensive final assembly.
- The MP3 is compact for listening and sharing; WAV is the lossless output for
  editing or archival use.

## Limitations

- Speech synthesis cannot be safely interrupted mid-inference, so pause and
  cancel take effect after the current chunk.
- Literal code narration is complete but is not a replacement for viewing source
  code visually.
- Diagrams without useful alt text, complex embedded HTML, and custom Markdown
  extensions can only be represented by their readable text or metadata.
- The included interface focuses on common English voice options. Expanded voice
  selection and multilingual document segmentation are sensible future work.
- The application is designed for one trusted local user and has no
  authentication. Keep the default loopback host for personal use.
