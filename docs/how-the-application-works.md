# SpeakMD: How the Application Works

A guide for developers who know TypeScript and Node.js and want to understand this Python application — both as a product and as a working example of Python structure, conventions, and tooling.

This document is based on the source in this repository. It does not invent features. When something is not specified in the code, that is stated explicitly.

---

## Table of contents

1. [Overview](#1-overview)
2. [Application architecture](#2-application-architecture)
3. [Project structure](#3-project-structure)
4. [Getting started and runtime model](#4-getting-started-and-runtime-model)
5. [Python concepts you need to know](#5-python-concepts-you-need-to-know)
6. [Application walkthrough](#6-application-walkthrough)
7. [File-by-file guide](#7-file-by-file-guide)
8. [Framework and dependency guide](#8-framework-and-dependency-guide)
9. [Request, data, and execution flows](#9-request-data-and-execution-flows)
10. [Testing](#10-testing)
11. [Configuration and environment](#11-configuration-and-environment)
12. [Python best practices](#12-python-best-practices)
13. [Application-specific recommendations](#13-application-specific-recommendations)
14. [Glossary of Python concepts](#14-glossary-of-python-concepts)
15. [Further reading](#15-further-reading)

---

## 1. Overview

**SpeakMD** is a local web application that turns Markdown documents into spoken audio. You drop a `.md` file into a browser page running on your machine. The application:

1. Parses the Markdown into speech-friendly narration (headings, tables, code, links, and so on).
2. Splits that narration into semantic chunks sized for a text-to-speech (TTS) model.
3. Synthesizes each chunk with the **Kokoro** speech model, saving a WAV file per chunk.
4. Concatenates those checkpoints into a final WAV, and optionally encodes an MP3 with `ffmpeg`.

Everything stays on the local machine. There are no accounts, no hosted APIs, and no database. Job state lives as JSON on disk next to the audio files.

If you think in Node.js terms: this is a small Express-like HTTP server plus a **single in-process worker thread**, with the filesystem as the persistence layer. It is closer to a desktop-adjacent local tool than to a multi-tenant SaaS backend.

### What problem it solves

Long documents cannot be sent to a TTS model in one shot. Models have length limits; synthesis is slow; a crash halfway through a 500-chunk document would be expensive if work had to start over. SpeakMD is built around that constraint:

- Chunks are independently synthesizable and independently stored.
- Pause, cancel, retry, and process restart reuse valid WAV checkpoints.
- One worker owns the loaded model so GPU/CPU memory stays predictable.

### What this is not

Confirmed from the code:

- Not a cloud service and not multi-user. There is no authentication.
- Not a general Markdown renderer. The parser exists to produce *spoken* language, not HTML.
- Not an async job queue in the Redis/BullMQ sense. Work is an in-process `queue.Queue` plus one `threading.Thread`.
- Not a TypeScript-style strictly typed system. Type hints exist, but they are optional at runtime.

---

## 2. Application architecture

SpeakMD has four layers that map reasonably onto a typical Node.js full-stack app:

| Layer | This app | Node.js analogue |
| --- | --- | --- |
| UI | A single static HTML file with inline CSS and JS | A Vite SPA, or a plain `index.html` served by Express |
| HTTP API | FastAPI routes in `app.py` | Express / NestJS controllers |
| Domain / jobs | `JobManager` + helpers | A service layer plus a worker |
| Persistence | Directories under `output/` (`job.json`, WAVs) | SQLite / Postgres / object storage |

There is no ORM, no Redis, no message broker, and no separate frontend build.

```text
Browser (static/index.html)
        |  HTTP (JSON + multipart upload)
        v
FastAPI + Uvicorn          (async request handlers)
        |
        |  in-process calls
        v
JobManager                 (thread-safe API + filesystem state)
        |
        |  queue.Queue
        v
Worker thread              (one job at a time)
        |
        +--> markdown_speech.narrate_markdown()
        +--> chunking.plan_chunks()
        +--> tts.KokoroRenderer.synthesize()
        +--> audio.write_wav_atomic() / combine_wav_chunks() / encode_mp3()
        |
        v
output/<job-id>/
    input.md
    job.json          <-- source of truth
    chunks/0001.wav
    final/<name>.wav
    final/<name>.mp3
```

### Design decisions visible in the architecture

**Single worker, one model.** `JobManager` docstring: *"A deliberately small persistent queue: one model, one renderer, one job at a time."* Loading Kokoro twice would double RAM/VRAM. The code never starts a pool of synthesizers.

**Filesystem as database.** Each job is a directory. `job.json` is replaced atomically after every state change. WAV checkpoints are written atomically *before* the JSON is updated, so a crash can recover a finished chunk whose status write did not land.

**HTTP stays thin.** Uploading a file only validates settings, writes `input.md` and an initial `job.json`, and enqueues an ID. Parsing and TTS run in the worker, not in the request handler. That is the same instinct as "don't do heavy work in the Express route; push it to a worker."

**The UI polls.** The page hits `/api/jobs/{id}` every second and `/api/metrics` every two seconds. There is no WebSocket and no Server-Sent Events.

### What is in-process vs external

| Concern | Where it lives |
| --- | --- |
| HTTP server | Uvicorn in this process |
| Job queue | `queue.Queue` in this process |
| TTS model | Lazy-loaded in the worker thread (Kokoro / PyTorch) |
| GPU detection for TTS | PyTorch `torch.cuda.is_available()` |
| GPU metrics for the UI | `nvidia-smi` subprocess, if present |
| MP3 encoding | `ffmpeg` subprocess, if present |
| Hugging Face model files | Downloaded by Kokoro on first use (`hexgrad/Kokoro-82M`) |

Kokoro and PyTorch are Python libraries imported in-process. `ffmpeg` and `nvidia-smi` are *external binaries* invoked with `subprocess`. If they are missing, the app continues: no MP3, or GPU metrics marked unavailable.

---

## 3. Project structure

```text
text-2-speech-engine/
├── pyproject.toml              Project metadata, dependencies, scripts, test config
├── uv.lock                     Locked transitive dependency graph (like package-lock.json)
├── README.md                   User-facing product documentation
├── .gitignore
├── src/speakmd/                The installable Python package (application code)
│   ├── __init__.py
│   ├── app.py                  FastAPI app, routes, process entrypoint
│   ├── jobs.py                 Persistent queue, worker, recovery
│   ├── markdown_speech.py      Markdown → narration blocks
│   ├── chunking.py             Narration blocks → TTS-sized chunks
│   ├── tts.py                  Kokoro (and test-tone) renderer
│   ├── audio.py                Atomic WAV I/O, concatenation, MP3
│   ├── monitor.py              CPU / RAM / GPU snapshots
│   └── static/index.html       The entire frontend
├── tests/
│   └── test_pipeline.py        Narration, chunking, pause/resume/recovery
├── docs/                       This guide
└── output/                     Runtime job data (gitignored)
```

There is no `package.json`, no `tsconfig.json`, no `src/index.ts`, no Dockerfile, no CI config, and no `conftest.py` in this repository.

### Why `src/speakmd/` instead of putting `.py` files at the repo root?

This is the **src layout**, a common modern Python convention.

In Node.js, `import './jobs'` from a file in the repo often just works because the working directory is part of the resolution story. In Python, the current directory is also on `sys.path` when you run a file directly, which makes it easy to accidentally import the *source tree* instead of the *installed package*. That breaks later, when the project is installed for real.

Putting the package under `src/` and installing it (this project uses `uv sync`, which creates `.venv` and installs `speakmd` in editable mode) means:

```python
from speakmd.jobs import JobManager
```

always refers to the package, the same way a well-configured Node project prefers `import { JobManager } from 'speakmd/jobs'` over deep relative hacks from the repo root.

`tests/` lives *outside* `src/`. Tests import `speakmd...` as an installed package, not as `from src.speakmd...`. That matches the pytest convention of keeping tests separate from the importable library.

### Directory roles

| Path | Role |
| --- | --- |
| `src/speakmd/` | The library *and* the web app. In Python it is normal for a package to be both. |
| `src/speakmd/static/` | Static assets shipped *inside* the package, located via `Path(__file__).parent`. |
| `tests/` | pytest collection root (`testpaths = ["tests"]` in `pyproject.toml`). |
| `output/` | Default job store. Created at import time. Gitignored. |
| `.venv/` | Virtual environment created by `uv sync`. Gitignored. Analogous to `node_modules` plus a dedicated Node binary. |
| `docs/` | Human documentation. Not imported by the application. |

### Important files at the repo root

**`pyproject.toml`** is the closest thing to `package.json` plus parts of `tsconfig.json` and `jest.config`. It declares the package name (`speakmd`), version, Python version constraint, runtime dependencies, optional dev dependencies, the console script `speakmd`, the Hatchling build backend, and pytest options. See [§8](#8-framework-and-dependency-guide) and [§7.1](#71-pyprojecttoml).

**`uv.lock`** is a lockfile generated by [uv](https://docs.astral.sh/uv/). Like `package-lock.json` or `pnpm-lock.yaml`, it pins every transitive package. You do not edit it by hand.

**`.gitignore`** ignores `.venv/`, bytecode caches (`__pycache__/`, `*.py[cod]`), pytest cache, `output/`, and `.DS_Store`. Runtime audio is not source.

---

## 4. Getting started and runtime model

### Python vs Node: environments and package installs

Node isolates dependencies per project in `node_modules` while using a globally installed `node`. Python traditionally uses a **virtual environment**: a project-local copy of the Python interpreter *and* its `site-packages`.

| Concept | Node.js | This project |
| --- | --- | --- |
| Manifest | `package.json` | `pyproject.toml` `[project]` table |
| Lockfile | `package-lock.json` | `uv.lock` |
| Install | `npm install` | `uv sync --extra dev` |
| Local deps folder | `node_modules/` | `.venv/` (interpreter + packages) |
| Run a project command | `npx speakmd` / `npm start` | `uv run speakmd` |
| Dev-only deps | `devDependencies` | `[project.optional-dependencies] dev` |
| Engine constraint | `"engines": { "node": ">=18" }` | `requires-python = ">=3.11,<3.13"` |

`uv` is a fast package and project manager (think of a single tool covering npm + nvm + a lockfile solver). `uv python install 3.12` is the nvm-like step. `uv sync` creates `.venv` and installs the graph from `uv.lock`.

Python **3.13 is excluded**. The code does not say why; a likely reason is native-extension support in the TTS stack (PyTorch / Kokoro), but that is an inference from the constraint, not a comment in the repo.

### How the process starts

Three equivalent entry points exist. All end up serving the FastAPI `app` object.

1. **Console script (documented in README):**

   ```bash
   uv run speakmd
   ```

   `pyproject.toml` contains:

   ```toml
   [project.scripts]
   speakmd = "speakmd.app:main"
   ```

   That is the Python counterpart of `"bin": { "speakmd": "dist/cli.js" }`. Installing the package places a `speakmd` executable on `PATH` (inside `.venv`) that calls `main()` in `speakmd.app`.

2. **Running the module file directly:**

   ```python
   if __name__ == "__main__":
       main()
   ```

   `__name__` is `"__main__"` only when that file is the program being executed, similar to `require.main === module` in Node. Relative imports still require the file to be part of the `speakmd` package, so the supported way is the console script, not `python src/speakmd/app.py` from a random directory.

3. **`main()` starts Uvicorn:**

   ```python
   uvicorn.run(
       "speakmd.app:app",
       host=os.environ.get("SPEAKMD_HOST", "127.0.0.1"),
       port=int(os.environ.get("SPEAKMD_PORT", "8000")),
       reload=False,
   )
   ```

   The string `"speakmd.app:app"` means "import module `speakmd.app` and take the attribute `app`". Uvicorn is an ASGI server — the Python equivalent of running an Express app with `http.createServer(app).listen(...)`, except the app speaks the **ASGI** protocol (asynchronous, like a standardized version of connecting a Node framework to a server).

   `reload=False` means there is no nodemon-style restart on file change.

### Lifespan: startup and shutdown

FastAPI is constructed with `lifespan=lifespan`. The lifespan function is an **async context manager**:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.manager = JobManager(OUTPUT_ROOT)
    app.state.monitor = ResourceMonitor()
    yield
    app.state.manager.shutdown()
```

Everything before `yield` runs at startup; everything after runs at shutdown. NestJS analogue: `onModuleInit` / `onModuleDestroy`. Express analogue: listen callback plus a `SIGTERM` handler.

`JobManager.__init__` immediately:

1. Creates the output directory.
2. Recovers interrupted jobs from disk (`_recover_jobs`).
3. Starts a daemon worker thread named `speakmd-worker`.

So by the time the first HTTP request arrives, the worker is already looping on the queue, and any job that was `processing` when the process died has been moved back to `queued`.

### Import-time side effects

Two things happen when `speakmd.app` is imported, *before* Uvicorn finishes booting:

```python
ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = Path(os.environ.get("SPEAKMD_OUTPUT", ROOT / "output")).expanduser().resolve()
...
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
app = FastAPI(...)
app.mount("/files", StaticFiles(directory=OUTPUT_ROOT), name="files")
```

`OUTPUT_ROOT` is computed once, from the environment at import time. Changing `SPEAKMD_OUTPUT` later in the same process would not move it. The directory is created as soon as the module loads.

`Path(__file__).resolve().parents[2]` walks from `src/speakmd/app.py` → `src/speakmd/` → `src/` → **repository root**. That is how the default `output/` folder lands next to `pyproject.toml` rather than inside the package.

### Request model vs worker model

FastAPI route functions in this app are declared `async def`. They run on Uvicorn's event loop (Python's `asyncio`, conceptually close to Node's event loop).

The TTS worker is **not** async. It is a standard OS thread using blocking calls: file I/O, Kokoro inference, `ffmpeg`. That mix is intentional and common in Python:

- HTTP stays responsive (polling, pause, metrics).
- The model is used from one thread, so there is no concurrent inference.
- PyTorch/Numpy work typically releases the GIL (see [§5](#the-gil-and-why-a-thread-is-acceptable-here)), so the event loop is not frozen for the whole synthesis.

This is *not* the same as Node worker threads (isolated V8 isolates) and *not* the same as a separate `worker.js` process. All threads share one Python interpreter and one memory space. Shared state is guarded with `threading.RLock`.

### How you actually use it

From the README, after `uv sync --extra dev`:

```bash
uv run speakmd
```

Open `http://127.0.0.1:8000`. Upload Markdown, choose voice / speed / device / chunk size, start conversion, watch progress, download WAV/MP3.

Optional system dependencies:

- `ffmpeg` on `PATH` for MP3. WAV is still produced without it.
- NVIDIA driver + CUDA-capable PyTorch for GPU synthesis. CPU works without it.

---

## 5. Python concepts you need to know

This section only covers concepts the codebase actually uses, with TypeScript comparisons. Later sections assume you have read it.

### Indentation is syntax

Python does not use `{ }` to delimit blocks. Indentation *is* the block structure:

```python
if requested == "cpu":
    return "cpu"
```

In TypeScript that is `if (requested === "cpu") { return "cpu"; }`. Mixing tabs and spaces, or dedenting too early, is a syntax error (`IndentationError`), not a style nit.

### Dynamic typing plus optional type hints

Variables are not declared with a type. This is legal:

```python
device = str(settings.get("device") or "auto")
```

Type hints are annotations. They look like TypeScript but **are not enforced at runtime** unless a library (Pydantic, FastAPI for request params) inspects them:

```python
def choose_device(requested: str = "auto") -> str:
```

is similar in *intent* to `function chooseDevice(requested: string = "auto"): string`. A checker such as mypy or Pyright can verify hints. This repo does not configure a type checker.

Modern syntax used here (Python 3.9–3.11+):

| Hint | Meaning | TypeScript |
| --- | --- | --- |
| `str` | string | `string` |
| `dict[str, Any]` | object/map with string keys | `Record<string, unknown>` |
| `list[str]` | array of strings | `string[]` |
| `str \| None` | string or null-like | `string \| null` |
| `Literal["auto", "cpu", "cuda"]` | string union of exact values | `"auto" \| "cpu" \| "cuda"` |
| `tuple[dict[str, Any], bool]` | fixed-length pair | `[Job, boolean]` |
| `Path` | filesystem path object | a branded `string` plus `fs`/`path` |

`None` is Python's `null`. There is no `undefined`. Missing dict keys raise `KeyError` unless you use `.get()`.

`from __future__ import annotations` at the top of most modules makes hints lazy (stored as strings). That allows forward references and keeps import-time cost down. It is a common boilerplate line; it does not change runtime behavior of the functions themselves.

### `def`, default arguments, and `->` returns

```python
def plan_chunks(blocks: list[NarrationBlock], max_chars: int = DEFAULT_MAX_CHARS) -> list[SpeechChunk]:
```

- `def` is `function`.
- Default arguments (`max_chars: int = DEFAULT_MAX_CHARS`) work like TS defaults. A Python caveat not triggered here: mutable defaults (`[]`, `{}`) are created *once* at function definition. This code uses immutable ints/strings as defaults, which is safe.
- `-> list[SpeechChunk]` is the return type hint.

Keyword arguments are used at call sites (`narrate_code(token.content, token.info.strip())` is positional; FastAPI uses many keyword-style defaults). You can call `plan_chunks(blocks, max_chars=180)` by name, similar to TS object-destructured options, except Python has real named parameters.

### Modules, packages, and imports

A **module** is a `.py` file. A **package** is a directory with `__init__.py` (this project uses that classic marker).

```python
from .jobs import JobManager
from .monitor import ResourceMonitor
```

The leading `.` is a **relative import**: "from the same package." It is the counterpart of `import { JobManager } from './jobs.js'`.

Absolute form used in tests:

```python
from speakmd.jobs import JobManager
```

like `import { JobManager } from 'speakmd/jobs'`.

`import json` / `import os` / `import threading` are the standard library, analogous to Node's built-in `fs`, `path`, `crypto`. There is no `import fs from 'fs'` split between CJS and ESM: Python has one import system. (There *is* a circular-import hazard, as in any language; this codebase's graph is a tree rooted at `app.py`.)

**`__init__.py`** here is two lines: a docstring. Its job is to mark `speakmd` as a package. It does not re-export symbols. Importing `speakmd` does not automatically import `app` or `jobs`.

There is **no `__main__.py`**. `python -m speakmd` is therefore not defined as an entry. The supported CLI is the `speakmd` script.

### Classes, methods, and `self`

```python
class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.RLock()
```

| Python | TypeScript |
| --- | --- |
| `__init__` | `constructor` |
| `self` | `this` (but must be the first parameter explicitly) |
| `self.root = root` | `this.root = root` |
| `_lock` leading underscore | convention for "internal" (not enforced, unlike TS `private`) |

`@staticmethod` (see `JobManager._validate_settings`) is a method that does not receive `self`. It is namespaced on the class but is really a function. Close to a `static` method in TS.

Instance attributes are created in `__init__` by assignment. There is no class field syntax required (though it exists in newer Python). `KokoroRenderer.sample_rate = 24000` is a **class attribute**, shared by all instances, like a `static` field.

### Dataclasses

```python
@dataclass(frozen=True)
class SpeechChunk:
    index: int
    text: str
    pause_after: float
    block_kinds: list[str]
    block_start: int
    block_end: int
```

`@dataclass` generates `__init__`, equality, and representation. `frozen=True` makes instances immutable (assigning `chunk.index = 2` would raise). `asdict(self)` turns the instance into a plain dict for JSON.

TypeScript analogue: a `readonly` interface plus a constructor, or a Zod object you parse once. Dataclasses are still runtime Python objects; they are not a separate type-level-only construct.

Decorator reminder: `@dataclass` is a function that takes the class and returns a modified class. FastAPI's `@app.get("/")` is the same idea: wrap a function, register it as a route. TS decorators are related but optional and differently specified; Python decorators are ordinary, heavily used syntax.

### Exceptions

Python uses exceptions for control flow more often than Node's "return `null` / throw."

```python
try:
    parsed_settings = json.loads(settings)
    ...
except (ValueError, json.JSONDecodeError) as exc:
    raise api_error(exc) from exc
```

| Python | JavaScript / TypeScript |
| --- | --- |
| `try / except / finally` | `try / catch / finally` |
| `raise ValueError("...")` | `throw new Error("...")` |
| `raise api_error(exc) from exc` | rethrow while chaining `cause` |
| `except Exception` | `catch (e)` of anything |
| `FileNotFoundError` | typically `ENOENT` wrapped by you |

`from exc` sets `__cause__`, so tracebacks show both errors. Bare `except Exception` in the worker is a last-resort guard so a bug in one job cannot kill the thread.

There is no `Error` subclass hierarchy required; the stdlib already provides `ValueError`, `RuntimeError`, `OSError`, and friends. FastAPI turns `HTTPException` into an HTTP response.

### `with` and context managers

```python
with open(temporary, "w", encoding="utf-8") as output:
    json.dump(value, output, ensure_ascii=False, indent=2)
    output.flush()
    os.fsync(output.fileno())
```

`with` calls setup and guaranteed teardown, even if the block raises. It is `try/finally` with a protocol (`__enter__` / `__exit__`). Node analogue: `await using` in modern TS, or a `finally { fh.close() }`.

The lifespan function is the async form: `@asynccontextmanager` + `yield`.

`wave.open(...)` is also used as a context manager, so WAV handles close automatically.

### `pathlib.Path`

Python's modern path API. Methods used in this repo:

- `Path(__file__)` — this file
- `.resolve()`, `.expanduser()`, `.parent` / `.parents[n]`
- `/` operator: `self.root / job_id` (joins paths; not division of numbers here)
- `.mkdir(parents=True, exist_ok=True)` — `mkdir -p`
- `.read_text()`, `.exists()`, `.is_dir()`, `.iterdir()`
- `.with_name(...)` — sibling path with a new filename

This is nicer than concatenating strings with `path.join` in Node, and it is the current Python convention.

### Comprehensions

Used throughout instead of `map`/`filter` chains:

```python
completed = [state for state in states if state["status"] == "completed"]
```

TypeScript: `states.filter(s => s.status === "completed")`.

Dict comprehensions:

```python
job["chunk_states"] = {
    f"{chunk.index:04d}": { ... }
    for chunk in chunks
}
```

Generator-like iteration appears in `ids()`:

```python
return sorted(
    directory.name
    for directory in self.root.iterdir()
    if directory.is_dir() and SAFE_ID.fullmatch(directory.name) and (directory / "job.json").exists()
)
```

That is a generator expression passed to `sorted`, not an intermediate list.

### f-strings and formatting

```python
f"{chunk.index:04d}"   # 1 → "0001"
f"Chunk {index} failed; retry is available"
```

Template literals in TS: `` `Chunk ${index} failed` ``. The `:04d` is printf-style padding, not something TS template strings provide natively.

### The walrus operator `:=`

```python
while frames := source.readframes(65536):
    output.writeframes(frames)
```

Assigns *and* yields the value, like `while ((frames = read()) !== '')` in JS. Here it reads PCM frames until an empty `bytes` object (falsy).

### `dict`, `list`, `set`, and JSON

Python `dict` is the object/map. Keys in job state are strings. `list` is a mutable array. `set` is used for `_enqueued` membership tests.

JSON round-trip uses the stdlib:

```python
json.loads(settings)          # parse
json.dump(value, output, ...) # serialize to a file
json.dumps(clean_settings, sort_keys=True).encode()  # stable hash input
```

Unlike TypeScript, there is no separate "interface vs runtime" gap for these dicts: the job *is* a dict. The cost is that keys are not checked by the compiler. A typo like `job["stat"]` fails at runtime.

`deepcopy(job)` (from `copy`) clones nested dicts/lists so `summary()` can strip `chunks` without mutating the stored object.

### Typing over dicts vs Pydantic

FastAPI often uses **Pydantic** models (runtime-validated classes, like Zod schemas that also generate TS-like types). This application mostly uses plain `dict[str, Any]` for job state and a JSON string for upload settings. FastAPI still parses path parameters and `UploadFile` using its own machinery. See [§8](#fastapi).

### Threads, queues, events, locks

```python
self._queue: queue.Queue[str] = queue.Queue()
self._lock = threading.RLock()
self._stop = threading.Event()
self._worker = threading.Thread(target=self._work, name="speakmd-worker", daemon=True)
```

| Python | Rough Node analogue |
| --- | --- |
| `queue.Queue` | a thread-safe in-memory queue (not Redis) |
| `threading.Thread` | `worker_threads` but *shared memory* |
| `RLock` | a reentrant mutex |
| `Event` | a boolean flag you can wait on |
| `daemon=True` | thread will not keep the process alive |

`RLock` is reentrant: the same thread can acquire it twice (needed because `create_job` holds the lock and calls `_enqueue`, which also acquires it).

`Queue.get(timeout=0.5)` raises `queue.Empty`, which the worker catches — a polling loop, not an async `await`.

### The GIL and why a thread is acceptable here

CPython has a Global Interpreter Lock: only one thread runs Python bytecode at a time. That makes threads a poor fit for *pure-Python* CPU loops, but a good fit when:

- Work is I/O bound, or
- Work happens in C/CUDA extensions that release the GIL (NumPy, PyTorch, file I/O).

Kokoro inference is in that second category. The worker thread can synthesize while Uvicorn's thread/event loop serves `/api/jobs/{id}`.

This is still **one job at a time** by policy, not because the GIL requires it. The policy exists to keep the model loaded once and to make pause/cancel simple.

### `async` / `await` in this codebase

Route handlers are `async def` and `await file.read()`. Lifespan is async. The worker, Markdown parser, chunker, TTS, and audio modules are **synchronous**.

Python `async` is cooperative, like JS. Mixing `async def` with blocking calls *inside the event loop* would freeze HTTP. SpeakMD avoids that by putting blocking TTS on another thread. The async routes themselves do little work.

### Environment variables

```python
os.environ.get("SPEAKMD_PORT", "8000")
```

Equivalent to `process.env.SPEAKMD_PORT ?? "8000"`. No `dotenv` package is used; you export vars in the shell. Tests use pytest's `monkeypatch.setenv`.

### Byte strings vs text

Python distinguishes `bytes` and `str`. Uploads arrive as `bytes`; Markdown is decoded as UTF-8 (with BOM support via `utf-8-sig`). Hashing uses `hashlib.sha256(value).hexdigest()` on bytes.

### Truthiness

Empty `""`, `[]`, `{}`, `0`, `None`, and `False` are falsy. The UI-facing code relies on this (`if not text.strip()`, `if warning:`). JS has a similar but not identical list (`""` is falsy in both; empty `[]` is *truthy* in JS and *falsy* in Python). That last difference matters if you port conditions blindly.

---

## 6. Application walkthrough

This section follows a document from browser to MP3, naming the modules involved. File-level detail is in [§7](#7-file-by-file-guide).

### 6.1 The UI

`src/speakmd/static/index.html` is a single-file page: no React, no bundler, no `node_modules`. FastAPI serves it at `GET /`.

On load it:

1. Fetches `/api/metrics` and starts a 2-second metrics poll.
2. Fetches `/api/jobs` and, if any jobs exist, renders the most recently updated one (`data.jobs[0]`; the API returns jobs sorted by `updated_at` descending).

Starting a conversion builds `FormData` with the file and a JSON string of settings (`voice`, `speed`, `device`, `max_chars`) and `POST`s `/api/jobs`. Then it polls `GET /api/jobs/{id}` every second until a terminal or paused state.

Pause / resume / retry / cancel are empty `POST`s to `/api/jobs/{id}/{action}`.

Completed files are linked as `/files/<relative-path>`, which is the `StaticFiles` mount of the output directory.

### 6.2 HTTP boundary

`app.py` validates that the filename looks like Markdown (`.md`, `.markdown`, `.mdown`, `.mkdn`), parses settings JSON, and calls `JobManager.create_job`. Failures become HTTP 400. Unknown jobs become 404.

`app.state.manager` is the process-wide `JobManager`. Helper `manager()` reads it. This is ad hoc dependency lookup, not FastAPI's `Depends()`.

### 6.3 Job identity and reuse

`create_job` hashes the raw file bytes and a canonical JSON dump of cleaned settings:

```text
job_id = {slugify(filename stem)}-{source_sha256[:12]}-{settings_hash[:8]}
```

Example from a real `output/` directory in this workspace:

```text
sadhana-yog-ttc-strategy-guide-2026-08-22-984451b6dd06-02096e4d
```

Uploading the *same bytes* with the *same settings* reopens that directory (`existing: true`) instead of synthesizing again. Changing voice, speed, device, `max_chars`, or the chunking version (baked into settings) produces a different settings hash and a new job.

The upload is capped at 100 MiB and must decode as UTF-8.

### 6.4 Queueing

The new job is written as `state: "queued"` with empty `chunks`, then the ID is put on `queue.Queue`. The HTTP response returns a **summary** (no full chunk texts).

The worker's `_work` loop:

1. `get`s an ID (0.5s timeout so it can notice shutdown).
2. Sets `_active_job`.
3. Calls `_process`.
4. On unexpected exceptions, marks the job `failed`.
5. Clears `_active_job` and control flags.

Empty string on the queue is the shutdown sentinel.

### 6.5 Prepare: Markdown → chunks

If `job["chunks"]` is already populated (resume / recovery), prepare is a no-op.

Otherwise `_prepare`:

1. Sets `preparing`, reads `input.md`.
2. Sets `parsing_markdown`, calls `narrate_markdown`.
3. Calls `plan_chunks` with `max_chars`.
4. Stores chunk dicts, per-chunk state (`pending`, path `chunks/0001.wav`, text hash, attempts), and warnings.

Narration is the interesting product logic: tables become self-contained spoken rows; code punctuation is named; URLs are spelled.

### 6.6 Reconcile checkpoints

Before synthesis, `_reconcile_checkpoints` compares `chunk_states` to files on disk:

| JSON status | WAV on disk | Result |
| --- | --- | --- |
| `completed` | missing/invalid | reset to `pending` |
| `pending` or `processing` | valid WAV | mark `completed` (crash after WAV, before JSON) |
| `processing` | no valid WAV | reset to `pending` |

This is the recovery algorithm. A kill -9 loses at most the in-flight chunk.

### 6.7 Synthesis loop

For each chunk not `completed`:

1. Honor pause/cancel flags **before** calling the model.
2. Mark `processing`, increment `attempts`, save JSON (so a crash shows which chunk was running).
3. `renderer.synthesize(text, voice, speed)` → float32 waveform.
4. If cancel arrived during inference, drop the audio and exit.
5. Append silence of `pause_after` seconds, `write_wav_atomic`.
6. Mark `completed`, record `render_seconds`.
7. If pause arrived, save and exit *keeping* this chunk.

The renderer is created once per device via `_renderer_for`. Switching device in a later job reconstructs it. `SPEAKMD_TTS_BACKEND=tone` substitutes a sine-wave renderer used by tests.

### 6.8 Final assembly

All chunk WAVs are concatenated with `combine_wav_chunks` (streaming frames, 64 KiB at a time). `encode_mp3` runs `ffmpeg` at 64 kb/s mono. Missing ffmpeg becomes a warning, not a failed job. Output paths stored in `job["output"]` are relative to `OUTPUT_ROOT` so the UI can build `/files/...` URLs.

### 6.9 Controls

| Action | If not yet rendering | If currently synthesizing |
| --- | --- | --- |
| Pause | Immediate `paused` | Flag; after current chunk WAV is saved → `paused` |
| Cancel | Immediate `cancelled` | Flag; in-flight audio discarded; completed chunks kept |
| Resume | Only from `paused` → `resuming`/`queued` | n/a |
| Retry | Failed chunks reset to `pending`; or retry assemble/prepare if the job failed without chunk errors | n/a |

The README's statement that synthesis cannot be interrupted *mid-inference* matches the code: flags are checked around `synthesize()`, not inside the model.

---

## 7. File-by-file guide

### 7.1 `pyproject.toml`

**Responsibility.** Project metadata, dependency declaration, CLI entry, build backend, pytest config.

**Why it exists.** Python's standard project file (PEP 621). Without it there is no installable package and no `speakmd` command.

**Important fields.**

- `name = "speakmd"` — import name and package name.
- `requires-python = ">=3.11,<3.13"` — 3.11 or 3.12 only.
- `dependencies` — runtime packages (see [§8](#8-framework-and-dependency-guide)).
- `optional-dependencies.dev` — `pytest` and `httpx`. Tests currently use pytest only; `httpx` is unused in `tests/test_pipeline.py`.
- `[project.scripts] speakmd = "speakmd.app:main"` — CLI.
- `[build-system]` Hatchling — builds wheels; uv uses this when installing the local package.
- `[tool.hatch.build.targets.wheel] packages = ["src/speakmd"]` — which package to include.
- `[tool.pytest.ini_options]` — `testpaths = ["tests"]`, `addopts = "-q"` (quiet).

**Depends on.** Nothing in the app; tools read it.

**Dependents.** uv, pip, hatchling, pytest.

**Python concepts.** Declarative project metadata instead of `setup.py`. Optional extras (`dev`) are installed with `uv sync --extra dev`, analogous to installing `devDependencies`.

### 7.2 `uv.lock`

**Responsibility.** Pin every direct and transitive dependency (FastAPI, Kokoro, PyTorch, NumPy, Hugging Face Hub, pytest, …).

**Why it exists.** Reproducible installs. Same role as `package-lock.json`.

**Do not document every package in this file.** Many exist only because Kokoro pulls a scientific-Python stack. The application imports a small subset directly.

### 7.3 `.gitignore`

Ignores virtualenv, bytecode, pytest cache, `output/`, and macOS folder metadata. Compiled `.pyc` files are not source; `output/` is user data.

### 7.4 `README.md`

Product documentation: features, pipeline diagram, install, run, env vars, output layout, tests, performance notes, limitations. This guide complements it; it does not replace it. If they ever diverge, **the code is the source of truth** for behavior, and the README is the source of truth for intended UX.

### 7.5 `src/speakmd/__init__.py`

```python
"""SpeakMD: local, resumable Markdown narration."""
```

Marks the directory as a package and sets the package docstring. It does not import submodules (so `import speakmd` is cheap and has no side effects such as loading FastAPI or Kokoro).

### 7.6 `src/speakmd/app.py`

**Responsibility.** Process entrypoint, FastAPI application, HTTP routes, static file mounts, lifespan wiring.

**Why it exists.** Boundary between the browser and `JobManager`. Keep HTTP concerns (status codes, multipart, static hosting) out of the job engine.

**Constants and helpers.**

| Name | Role |
| --- | --- |
| `ROOT` | Repository root (two parents above this file) |
| `OUTPUT_ROOT` | Job store; env `SPEAKMD_OUTPUT` or `ROOT / "output"` |
| `STATIC_ROOT` | `src/speakmd/static` |
| `lifespan` | Construct `JobManager` and `ResourceMonitor`; shutdown manager |
| `manager()` | `app.state.manager` accessor |
| `api_error` | Wrap any `Exception` as HTTP 400 with `str(exc)` |
| `main()` | `uvicorn.run(...)` |
| `app` | The FastAPI instance, also mounted at `/files` |

**Routes.**

| Method | Path | Behavior |
| --- | --- | --- |
| `GET` | `/` | `FileResponse` of `index.html` |
| `POST` | `/api/jobs` | Multipart `file` + form field `settings` (JSON string, default `"{}"`) |
| `GET` | `/api/jobs` | `{ jobs, queue }` |
| `GET` | `/api/jobs/{job_id}` | Job summary or 404 |
| `POST` | `/api/jobs/{job_id}/pause` | Pause |
| `POST` | `/api/jobs/{job_id}/resume` | Resume |
| `POST` | `/api/jobs/{job_id}/cancel` | Cancel |
| `POST` | `/api/jobs/{job_id}/retry` | Retry failed work |
| `GET` | `/api/metrics` | CPU/RAM/GPU plus queue fragment |

`File(...)` and `Form(...)` come from FastAPI. `File(...)` means required upload (the `...` is Python's `Ellipsis`, used by FastAPI as "required"). Settings default to the string `"{}"`.

Extension check uses `Path(filename).suffix.lower()`. There is no content-type sniffing of the bytes beyond UTF-8 decode inside `create_job`.

**Static files.** `app.mount("/files", StaticFiles(directory=OUTPUT_ROOT))` exposes the entire output tree. Combined with no authentication, that is acceptable only on loopback for a single trusted user, which is how the README frames the app.

**Error mapping.** `FileNotFoundError` and some `ValueError`s on get → 404. Validation `ValueError` / `JSONDecodeError` → 400. Cancel does not treat `ValueError` as 400 because `cancel()` does not raise it for terminal states; it just returns the summary.

**Depends on.** FastAPI, Uvicorn (inside `main` only — a **lazy import**), `JobManager`, `ResourceMonitor`.

**Dependents.** Uvicorn, the browser, pytest does *not* import this module today.

**Python concepts demonstrated.**

- Module-level constants computed at import.
- `async def` route handlers.
- `await file.read()` — FastAPI `UploadFile` is async.
- `raise HTTPException(...) from exc` — exception chaining.
- `if __name__ == "__main__"`.
- Lazy import of uvicorn inside `main()` so importing `app` for type-checking or tests would not require starting a server (tests currently skip this module anyway).

**Non-obvious details.**

- `reload=False` is explicit. Uvicorn's reloader would otherwise spawn two processes and two workers/models.
- Host defaults to `127.0.0.1`, not `0.0.0.0`.
- `list_jobs` does not paginate; every job directory with a valid `job.json` is loaded and summarized on each request.

### 7.7 `src/speakmd/jobs.py`

**Responsibility.** Durable job state, queue, worker, recovery, pause/resume/cancel/retry, progress summaries.

**Why it exists.** This is the application's core. HTTP, Markdown, TTS, and WAV are plugins around this state machine.

**Module-level constants.**

```python
JOB_SCHEMA_VERSION = 1
ACTIVE_STATES = {"queued", "preparing", "parsing_markdown", "processing", "resuming"}
TERMINAL_STATES = {"completed", "cancelled", "failed"}
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,100}$")
```

`SAFE_ID` prevents path traversal via `job_id` (`../etc` will not match). `paused` is intentionally *not* in `ACTIVE_STATES` or `TERMINAL_STATES`; recovery does not auto-resume a user pause.

**Functions.**

| Name | Role |
| --- | --- |
| `now()` | UTC ISO-8601 timestamp (`datetime.now(UTC).isoformat()`) |
| `slugify` | Lowercase, non-alphanumerics to `-`, max 48 chars |
| `sha256_bytes` | Hex digest |
| `atomic_json` | Write temp file, `fsync`, `os.replace` |

`os.replace` is atomic on POSIX for the same filesystem — the same idea as writing `file.tmp` and `renameSync` in Node.

#### `JobStore`

Small filesystem adapter:

- `path(job_id)` → `output_root / job_id` after `SAFE_ID` check.
- `load` / `save` under an `RLock`.
- `ids()` lists directories that look like jobs.

`save` always stamps `updated_at`.

#### `JobManager`

**Constructor state.** Output root, `JobStore`, `Queue`, `_enqueued` set (dedupes queue puts), `_controls` map (`job_id → "pause"|"cancel"`), `RLock`, stop `Event`, `_active_job`, lazy `_renderer` / `_renderer_device`, worker thread.

**Public API.** `create_job`, `get`, `list`, `summary`, `queue_status`, `request_pause`, `resume`, `cancel`, `retry_failed`, `shutdown`.

**`create_job(filename, raw_markdown, settings) -> tuple[dict, bool]`.** The bool is `existing`. Validation:

- Size ≤ 100 MiB.
- UTF-8 (utf-8-sig decode).
- `_validate_settings`:
  - `voice` matches `[a-z]{2}_[a-z0-9_]+` (Kokoro names like `af_heart`).
  - `speed` float in `[0.5, 2.0]`, default `1.0`.
  - `device` in `{auto, cpu, cuda}`, default `auto`.
  - `max_chars` int in `[160, 450]`, default `DEFAULT_MAX_CHARS` (360).
  - Injects `chunking_version` so planner changes invalidate the cache.

New jobs create `chunks/` and `final/`, atomically write `input.md`, then `job.json`.

**`summary`.** Deep-copies the job, **pops `chunks`**, attaches at most 260 characters of the current chunk's text, computes `percentage` and `eta_seconds` (`render_seconds / completed * remaining`). That keeps polling payloads small once a document has hundreds of chunks. The on-disk `job.json` still contains every chunk's full text (the sample job in `output/` is thousands of lines).

**Controls.** `_controls` is in-memory only. After a process restart, a pause requested mid-chunk but not yet honored is gone; the job is recovered as `queued` and will continue unless it had already been saved as `paused`. That is consistent with "JSON is the source of truth": an unsaved pause flag does not survive.

**`_process`.** Prepare → reload JSON (pause/cancel during parse) → reconcile → load renderer → loop chunks → combine WAV → MP3 → `completed`.

**Renderer reuse.** `_renderer_for(device)` rebuilds only when device changes. Jobs with `device: auto` that resolve to CUDA share one `KokoroRenderer`.

**Last-resort `except Exception`.** Prevents a bug from killing `_work`. A nested `except Exception: pass` around the failure write means a broken store could hide the original error — a tradeoff for worker immortality.

**Depends on.** `audio`, `chunking`, `markdown_speech`, `tts`, stdlib (`json`, `hashlib`, `queue`, `threading`, `pathlib`, …). **NumPy is imported inside the chunk loop** (`import numpy as np`) to concatenate audio with silence. That import is deferred so tests using the tone backend still pay for NumPy (it will be present via Kokoro's dependency tree when the package is installed) but `jobs.py` does not import NumPy at module load.

**Dependents.** `app.py`, `tests/test_pipeline.py`.

**Python concepts.** Classes, `RLock`, daemon threads, `queue.Queue`, dict-as-document, atomic file replace, comprehensions, f-strings, exception guards, `tuple` return, `@staticmethod`.

**Non-obvious details.**

- Tuple assignment: `job["state"], job["stage"] = "cancelled", "Cancelled"` assigns two keys in one statement.
- `list(dict.fromkeys([...]))` used when appending MP3 warnings: unique-preserving concatenation.
- `progress.current` is a 1-based chunk index for the UI.
- Failed synthesis **stops the job** at the first failed chunk (`return` after marking `failed`). Later chunks are left `pending`. Retry only resets `failed` states, then continues.

### 7.8 `src/speakmd/markdown_speech.py`

**Responsibility.** Convert Markdown source into a list of `NarrationBlock`s and optional warnings. This is the "document understanding" layer.

**Why it exists.** TTS engines speak linear text. Markdown structure (tables, emphasis, task lists, footnotes) must be *described*, not stripped. Silent dropping of code or table headers would make the audio unusable.

**Data types.**

```python
@dataclass(frozen=True)
class NarrationBlock:
    kind: str
    text: str
    level: int = 0
    table_number: int | None = None
    row_number: int | None = None

@dataclass(frozen=True)
class NarratedDocument:
    blocks: list[NarrationBlock]
    warnings: list[str]
```

`kind` values produced include: `heading`, `paragraph`, `blockquote`, `list_item`, `table_intro`, `table_row`, `code`, `thematic_break`, `html`, `metadata`, `footnote`, `inline`, `other`.

**Parser stack.** `markdown-it-py` with CommonMark, `linkify` and `typographer`, plus enabled `table` and `strikethrough`, plus plugins:

- `front_matter_plugin` — YAML front matter becomes a short spoken notice, not raw YAML.
- `footnote_plugin`
- `tasklists_plugin`

`markdown-it-py` is a Python port of the JavaScript `markdown-it` library. If you have used `markdown-it` in Node, the token stream (`heading_open`, `inline`, `fence`, …) is the same mental model.

**`render_inline`.** Walks inline tokens:

| Token | Spoken form |
| --- | --- |
| text | as-is |
| breaks | space |
| `code_inline` | "inline code: …" with punctuation named |
| links | visible text plus "link target" + spelled URL |
| images | "Image: {alt}, source {url}" |
| task-list checkbox HTML | "completed task" / "incomplete task" |
| other inline HTML | readable text via `_TextOnlyHtml` |
| strikethrough | "text marked deleted" … "end deleted text" |
| footnote ref | "footnote {label}" |
| emphasis/strong open/close | ignored (prosody, not extra words) |

**`_spoken_url`.** Parses with `urllib.parse.urlparse`, speaks `www`, dots, slashes, query strings ("question mark"), mailto `@` as "at". The TTS model is not asked to guess how to pronounce `https://example.com/a-b`.

**`narrate_code`.** "Code block in python." then "Line 1: …" with camelCase split (`helloWorld` → `hello World`) and a punctuation lexicon (`_CODE_PUNCTUATION`).

**Tables (`_consume_table`).** Walks tokens from `table_open` to `table_close`. Produces one `table_intro` ("Table N has K columns: … It has M data rows.") and one `table_row` per body row with **repeated header names**:

```text
Table 1, row 2: Name: Sarah. Age: 28. Location: Paris.
```

Missing headers get "Column N". Extra cells get extra "Column N" pairs. That repetition is why a single row can be retried or listened to in isolation.

**Block walk in `narrate_markdown`.** A manual index loop (`while i < len(tokens)`) because tables consume a variable token span. List stack tracks ordered vs bullet and item numbers. Quote depth and footnote depth choose prefixes (`Quotation:`, `Footnote:`).

Unknown tokens with content produce a warning and an `other` block rather than disappearing.

**`_TextOnlyHtml`.** Subclasses `html.parser.HTMLParser`. `handle_data` collects text nodes. Malformed HTML falls back to a regex tag strip.

**Depends on.** `markdown-it-py`, `mdit-py-plugins`, stdlib `html`, `html.parser`, `urllib.parse`, `re`.

**Dependents.** `jobs._prepare`, `chunking` (type `NarrationBlock` only), tests.

**Python concepts.** Dataclasses, subclassing, iterators (`Iterable[Token]`), tuple unpacking `table_blocks, i = _consume_table(...)`, `list[dict[str, int | bool]]` for the list stack, `dict.fromkeys` to unique warnings.

**Non-obvious details.**

- Headings consume the following `inline` token and `continue` without the normal `i += 1` falling through incorrectly; they do `i += 2` then `continue`.
- Nested lists share `list_stack`; an inner list is a new stack frame.
- Front matter is not parsed as YAML key-value speech; it is only "Document metadata is present."

### 7.9 `src/speakmd/chunking.py`

**Responsibility.** Pack `NarrationBlock`s into `SpeechChunk`s under a character ceiling, with structure-aware pauses.

**Why it exists.** Kokoro (and TTS generally) should receive moderate-length utterances. Naive `text[i:i+360]` would split mid-word and mid-table cell. This planner prefers block boundaries, then sentences, then words, then a hard split of a pathological token.

**Constants.** `CHUNKING_VERSION = "2026-08-semantic-v1"` (stored in job settings), `DEFAULT_MAX_CHARS = 360`.

**`SpeechChunk` fields.** `index` (1-based), `text`, `pause_after` (seconds of silence after synthesis), `block_kinds` (unique kinds in the chunk), `block_start` / `block_end` (1-based block indices in the narrated document).

**`split_block`.** Collapse whitespace, split into sentences via `_SENTENCES` (Latin and some CJK end punctuation), then `_words_under_limit` for oversize sentences.

**`plan_chunks` rules**, from the docstring and the loop:

- Headings start a new chunk but may share it with following prose (so "Heading level 2: Budget." is heard with the next paragraph).
- If a heading itself needed multiple parts, flush after that heading.
- `code` and `thematic_break` flush before (if something is open) and after, so they do not blend with unrelated text.
- Oversize table rows / code get a continuation prefix: `"Table {n}, row {m}, continued. "` or `"Code continues. "`. `part_limit` subtracts that prefix length so the continuation cannot exceed `max_chars`.
- `max_chars < 80` raises `ValueError`. The HTTP API never allows below 160; 80 is the planner's own safety floor.

**Pauses.** Heading 0.55s, thematic break 0.60s, paragraph/quote/html 0.35s, list item 0.25s, table row 0.20s, etc.

**Depends on.** `NarrationBlock`, stdlib `re`, dataclasses.

**Dependents.** `jobs`, tests.

**Python concepts.** Nested function `flush()` closing over `text_parts`, `kinds`, `start`, `end` with `nonlocal`. Frozen dataclass. `enumerate(blocks, start=1)`.

### 7.10 `src/speakmd/tts.py`

**Responsibility.** Hardware selection and speech rendering. Production path is Kokoro; test path is a sine wave.

**Why it exists.** Isolate model loading, device quirks, and the test double from the job loop.

**`Device`.** `Literal["auto", "cpu", "cuda"]`.

**`choose_device`.** `cpu` always CPU. Otherwise try `import torch` and `torch.cuda.is_available()`. Any exception → no CUDA. Requesting `cuda` without GPU raises `RuntimeError`. `auto` falls back to CPU.

**`KokoroRenderer`.**

- `sample_rate = 24000`.
- Lazy `KPipeline` per language code, cached in `_pipelines`.
- `language_for_voice`: first character of the voice name if it is one of `a b e f h i j p z`, else `"a"`. That matches Kokoro's convention (`af_heart` → American English `a`).
- `KPipeline(lang_code=..., repo_id="hexgrad/Kokoro-82M", device=self.device)` — first use downloads weights via Hugging Face Hub (network). The repo does not vendor the model.
- `synthesize` iterates the pipeline (Kokoro may split by phoneme limit), accepts either a Result object with `.audio` or a legacy tuple, moves tensors to CPU NumPy, inserts 70ms of silence between pieces, concatenates.

**`ToneRenderer`.** Deterministic sine at 220 Hz, duration from text length and speed, optional `SPEAKMD_TONE_DELAY` sleep so tests can observe `processing` before completion.

**`make_renderer`.** Factory: env `SPEAKMD_TTS_BACKEND=tone` → `ToneRenderer`, else `KokoroRenderer`.

**Depends on.** Optional `torch` and `kokoro` (imported inside functions — if missing, errors are delayed until first synthesis). NumPy inside `synthesize`.

**Dependents.** `JobManager._renderer_for`.

**Python concepts.** Lazy imports, `Literal`, factory function, class vs instance attributes, `getattr` for compatibility with two Kokoro result shapes.

**Non-obvious details.** Empty/whitespace text raises `ValueError`. If Kokoro yields nothing, `RuntimeError`. ImportError for kokoro is rewritten as a message telling the user to `uv sync`.

### 7.11 `src/speakmd/audio.py`

**Responsibility.** Atomic mono 16-bit WAV checkpoints, streaming concatenation, optional MP3.

**Why WAV.** Uncompressed PCM can be validated with the stdlib `wave` module and concatenated without decoding. That makes checkpoints cheap to verify after a crash.

**`write_wav_atomic`.** Clip float samples to `[-1, 1]`, scale to int16 little-endian (`<i2`), write a temp `.part-{pid}.wav`, fsync, `os.replace`. `finally` deletes the temp (`missing_ok=True` covers the success path where replace already moved it — `unlink` after replace may no-op if replace moved the inode; on POSIX `replace` unlinks the destination and the temp path may no longer exist, hence `missing_ok`).

**`valid_wav`.** Mono, 2-byte samples, at least one frame, optional sample-rate check. Failures (`EOFError`, `wave.Error`, `OSError`) → `False`. Incomplete files from a crash are invalid.

**`combine_wav_chunks`.** Reads format from the first file, streams 65536-frame blocks from each chunk, same atomic temp/replace pattern. Incompatible rates/channels/widths raise `ValueError`.

**`encode_mp3`.** `shutil.which("ffmpeg")`; if missing, returns a warning string. Runs:

```text
ffmpeg -hide_banner -loglevel error -y -i <wav> -c:a libmp3lame -b:a 64k <tmp.mp3>
```

Non-zero exit → warning with stderr. Success → `os.replace` to the final path. Never raises for a missing encoder; the job can still complete with WAV only.

**Depends on.** stdlib `wave`, `subprocess`, `shutil`, `os`, `pathlib`; NumPy only inside `write_wav_atomic`.

**Dependents.** `jobs.py`.

**Python concepts.** `with wave.open`, `subprocess.run(..., capture_output=True, text=True, check=False)`, walrus loop, `Path.unlink(missing_ok=True)`.

### 7.12 `src/speakmd/monitor.py`

**Responsibility.** Best-effort resource snapshot for the UI.

**`ResourceMonitor.snapshot`.** `psutil.cpu_percent(interval=None)` (non-blocking; constructor primes it once), virtual memory used/total/percent, GPU via `_gpu()`.

**GPU.** Cached for 1 second (`time.monotonic()`). Runs `nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits` with a 0.7s timeout. Parses CSV rows into device dicts. Any failure → `{ available: False, reason }`.

This is **not** the same as PyTorch CUDA availability. You can have `nvidia-smi` and still fail `torch.cuda.is_available()`, or the reverse on a broken install. The UI GPU tile can disagree with whether TTS uses CUDA.

**Depends on.** `psutil`, `subprocess`, `shutil`.

**Dependents.** `app.py` metrics route.

**Python concepts.** Simple caching with timestamps, defensive `except` around optional hardware.

### 7.13 `src/speakmd/static/index.html`

**Responsibility.** The entire frontend.

**Why one file.** Zero frontend toolchain. The Python package can serve it with `FileResponse`. For a local single-user tool this is a reasonable trade: no `npm run build` in the Python workflow.

**Behavior notes that affect the backend contract.**

- Settings posted as a **string** field named `settings`, not as individual form fields. That is why `app.py` does `json.loads(settings)`.
- Polling 1s / metrics 2s; no backoff.
- Pause button enabled only when `state === 'processing'` (not during `preparing` / `parsing_markdown`). The API *can* pause those states immediately; the UI simply does not offer the button then.
- Retry enabled only when `state === 'failed'`.
- Restores `jobs[0]` on load — the latest updated job, not necessarily the one this browser started.
- `fileUrl` encodes each path segment for `/files/...`.
- Shows at most 16 checkpoint links even if hundreds exist.
- Voice list is hardcoded (six English Kokoro voices). The backend regex allows any `xx_name` Kokoro voice; the UI does not enumerate the model.

**Depends on.** The JSON shapes from `/api/jobs` and `/api/metrics`.

**Dependents.** None in Python; the browser is the consumer.

### 7.14 `tests/test_pipeline.py`

Covered in [§10](#10-testing).

### 7.15 `output/` (runtime, not source)

Created at runtime. Shape:

```text
output/<job-id>/
  input.md
  job.json
  chunks/0001.wav
  chunks/0002.wav
  ...
  final/<output_name>.wav
  final/<output_name>.mp3
```

`job.json` is pretty-printed (`indent=2`) UTF-8 JSON. `chunk_states` keys are four-digit strings (`"0001"`). `output.wav` / `output.mp3` are paths relative to `OUTPUT_ROOT`.

This directory is gitignored; local copies you see in a workspace are artifacts of running the app, not part of the program.

---

## 8. Framework and dependency guide

Direct runtime dependencies from `pyproject.toml`:

| Package | Role in SpeakMD |
| --- | --- |
| `fastapi` | HTTP framework, routing, uploads, static files, HTTPException |
| `uvicorn[standard]` | ASGI server (`[standard]` adds extras like `httptools`, `uvloop` where available) |
| `markdown-it-py` | Markdown token parser |
| `mdit-py-plugins` | Front matter, footnotes, task lists |
| `kokoro` | TTS model wrapper (pulls torch, numpy, huggingface-hub, …) |
| `psutil` | CPU and RAM metrics |
| `python-multipart` | Required by FastAPI for `UploadFile` / form parsing |

Dev extra: `pytest`, `httpx`.

Not a Python package but required for full product behavior: `ffmpeg` binary. Optional: `nvidia-smi`.

### FastAPI

FastAPI is a Python web framework built on Starlette (HTTP/ASGI) and Pydantic (data validation). Comparisons:

| | Express | NestJS | FastAPI in this app |
| --- | --- | --- | --- |
| Route declaration | `app.get('/path', fn)` | `@Get()` on a controller | `@app.get("/path")` on a function |
| Validation | manual / Zod | DTOs + class-validator | Could use Pydantic models; this app mostly uses `dict` + hand validation |
| Async | native | native | `async def` handlers |
| OpenAPI | add Swagger yourself | optional | **automatic** at `/docs` (FastAPI default). SpeakMD does not customize it, but Uvicorn will serve Swagger UI unless disabled — the code does not disable it. |
| DI | modules / factories | constructors | `Depends()` exists; **this app uses `app.state` instead** |
| Static files | `express.static` | `ServeStaticModule` | `StaticFiles` + `FileResponse` |

**Routing.** Functions become routes through decorators. Path parameters (`job_id: str`) are parsed from the URL. Return values are JSON-encoded unless you return a `FileResponse`.

**Uploads.** `UploadFile = File(...)` plus `settings: str = Form("{}")` is multipart, like `multer` plus extra fields. `python-multipart` must be installed or FastAPI raises at request time.

**Lifecycle.** `lifespan` is the supported replacement for deprecated `@app.on_event("startup")`.

**Errors.** `HTTPException(status_code=400, detail="...")` becomes `{"detail": "..."}`. The UI reads `data.detail`.

**What FastAPI features this app does *not* use.** `Depends()`, Pydantic `BaseModel` for jobs, middleware, WebSockets, background `BackgroundTasks` (jobs use a dedicated thread instead), authentication, CORS middleware (same-origin loopback), routers split across modules (`APIRouter`).

### Uvicorn

ASGI server. `uvicorn.run("speakmd.app:app", ...)` is the analogue of `app.listen(8000)` after creating an HTTP server. `[standard]` extra installs optional high-performance pieces; the application code does not reference them directly.

### markdown-it-py and mdit-py-plugins

Python port of JS `markdown-it`. You enable features with `.enable(["table", "strikethrough"])` and `.use(plugin)`, the same names as in Node. Tokens have `.type`, `.content`, `.children`, `.attrGet("href")`.

SpeakMD does **not** render HTML for the browser. It only consumes the token stream.

### Kokoro

`kokoro.KPipeline` is the synthesis API. Application usage:

```python
pipeline = KPipeline(lang_code=language, repo_id="hexgrad/Kokoro-82M", device=self.device)
for result in pipeline(text, voice=voice, speed=speed):
    audio = result.audio  # or legacy tuple[2]
```

Weights come from Hugging Face (`hexgrad/Kokoro-82M`) on first load. That is an **external network service at first run**, even though synthesis afterwards is local. The README's "no hosted services" refers to SpeakMD not providing a cloud API, not to zero third-party model downloads.

PyTorch is a transitive dependency. `choose_device` imports `torch` only when needed.

### NumPy

Not listed in `pyproject.toml` but imported in `tts.py`, `audio.py`, and `jobs.py`. It arrives via Kokoro. Arrays are `float32` waveforms; checkpoints convert to int16 PCM.

### psutil

Cross-platform process and system stats. `cpu_percent(interval=None)` is non-blocking (returns since last call). `virtual_memory()` supplies RAM figures.

### python-multipart

No direct import in application code. FastAPI needs it to parse `multipart/form-data`.

### pytest and httpx

pytest is the test runner (Jest analogue). `httpx` is an HTTP client commonly used with FastAPI's `TestClient`. It is declared but **not imported in tests**. The suite tests `JobManager` in-process, not HTTP.

### Standard library used heavily

`json`, `os`, `re`, `hashlib`, `pathlib`, `threading`, `queue`, `wave`, `subprocess`, `shutil`, `html`, `urllib.parse`, `dataclasses`, `datetime`, `copy`, `time`, `contextlib`. A Node developer should treat this as Python's standard library being much broader than Node's core — WAV parsing and HTML parsing are built in.

### Alternatives (context only)

| Used here | Common alternative | Why this app's choice fits |
| --- | --- | --- |
| FastAPI | Flask, Django, Starlette alone | FastAPI is the current default for small APIs; uploads and JSON are easy |
| Uvicorn | Hypercorn, Gunicorn+Uvicorn workers | One local user; extra workers would duplicate the model (README warns against this) |
| markdown-it-py | mistune, markdown, pandoc | Token stream matches GFM tables well |
| Kokoro | OpenAI TTS API, Coqui, Piper | Local, no account; README positions the product as local-first |
| Files as DB | SQLite | Jobs *are* directories of audio; colocating JSON is simpler than blob storage |
| threads + queue | Celery, ARQ, Redis | Single machine, single model; a broker would be ceremony |
| uv | pip + venv, Poetry, PDM | Fast, lockfile, script runner |

---

## 9. Request, data, and execution flows

### 9.1 Upload and start

```text
UI: FormData(file, settings JSON string)
  POST /api/jobs
    app.upload_markdown
      validate extension
      json.loads(settings)
      JobManager.create_job
        validate UTF-8, size, settings
        job_id = slug + hashes
        if job.json exists: return summary, existing=true
        write input.md atomically
        write job.json (queued, empty chunks)
        enqueue id
        return summary, existing=false
UI: store job.id, poll GET /api/jobs/{id}
```

### 9.2 Worker processing

```text
_work loop
  id = queue.get()
  _process(id)
    load job.json
    skip if terminal or paused
    _prepare
      if no chunks:
        read input.md
        narrate_markdown → blocks + warnings
        plan_chunks → SpeechChunk list
        persist chunks + chunk_states
    reload job.json   # pause/cancel during parse
    _reconcile_checkpoints
    renderer = Kokoro or Tone (cached)
    for each chunk:
      skip completed
      honor pause/cancel
      synthesize
      honor cancel (drop) / else write WAV + silence
      honor pause (keep)
    honor pause/cancel before assemble
    combine_wav_chunks → final WAV
    encode_mp3 → warning or file
    state = completed
```

### 9.3 Pause during synthesis

```text
UI POST /pause
  if queued/preparing/parsing_markdown/resuming:
    state = paused (JSON)
  else:
    _controls[id] = "pause"
    stage = "Pause requested; finishing the current TTS call"
Worker, after WAV write:
  if control == pause:
    state = paused
    return
```

### 9.4 Crash and restart

```text
Process dies mid-chunk N (WAV not finished)
  leftover: incomplete temp file (ignored), chunk N still pending/processing
Uvicorn starts, JobManager.__init__
  _recover_jobs:
    processing → queued, stage = recovered, enqueue
Worker _reconcile:
  completed without valid WAV → pending
  pending/processing with valid WAV → completed
  processing without WAV → pending
Synthesis resumes at first non-completed chunk
```

### 9.5 Data shapes

**Settings (client → server, then stored):**

```json
{
  "voice": "am_adam",
  "speed": 1.15,
  "device": "auto",
  "max_chars": 360,
  "chunking_version": "2026-08-semantic-v1"
}
```

`chunking_version` is added server-side.

**Job summary (API):** stored job minus `chunks`, plus `current_chunk_text`, `queue`, `progress.percentage`, `progress.eta_seconds`.

**Chunk state entry:**

```json
{
  "status": "completed",
  "audio": "chunks/0001.wav",
  "text_sha256": "…",
  "attempts": 1,
  "error": null,
  "render_seconds": 1.32
}
```

`text_sha256` is stored but **never compared** in the current worker code. Reconciliation uses WAV validity and status, not hash mismatch. A future planner change is instead handled by a new settings hash / job id via `chunking_version`.

**Metrics:**

```json
{
  "cpu_percent": 12.0,
  "ram": { "used": 0, "total": 0, "percent": 0 },
  "gpu": { "available": false, "reason": "nvidia-smi not found" },
  "tts": { "active_job": null, "waiting": 0 }
}
```

(`used`/`total` are real byte counts at runtime; zeros here are schematic.)

### 9.6 Module dependency graph

```text
app.py
  ├── jobs.py
  │     ├── markdown_speech.py
  │     ├── chunking.py  →  markdown_speech.NarrationBlock
  │     ├── tts.py
  │     └── audio.py
  └── monitor.py

static/index.html  (HTTP only)

tests/test_pipeline.py
  ├── markdown_speech.py
  ├── chunking.py
  └── jobs.py  (which pulls the rest)
```

No cycles. `tts` and `audio` do not import `jobs`.

---

## 10. Testing

### How to run

```bash
uv run pytest
```

pytest discovers `tests/test_pipeline.py` because of `testpaths = ["tests"]` and the `test_*.py` naming convention (Jest's default `*.test.ts` analogue). `-q` is always passed via `addopts`.

There is no coverage config, no CI workflow in the repo, and no FastAPI `TestClient` tests.

### pytest vs Jest

| Jest | pytest in this file |
| --- | --- |
| `test('name', () => { ... })` | `def test_name():` |
| `expect(x).toBe(...)` | `assert x == ...` / `assert "substr" in text` |
| `beforeEach` | not used |
| `tmp` directories | fixture `tmp_path: Path` (injected by argument name) |
| `vi.stubEnv` | `monkeypatch.setenv(...)` (fixture by argument name) |
| `jest.setTimeout` | `wait_for(..., timeout=8)` helper |

**Fixtures by parameter name** are a pytest hallmark. Declaring `tmp_path: Path` in the test signature is enough: pytest injects a unique temporary directory. `monkeypatch` injects an object that can set env vars for the duration of the test. There is no `conftest.py`; these are built-in fixtures.

`from __future__ import annotations` is used in the test module as well.

### The four tests

**`test_narrator_retains_common_markdown_content`.** A fixture Markdown document covering front matter, headings, emphasis, inline code, links, blockquote, ordered list, task list, image, fenced Python, a table, strikethrough, Unicode (café, Hindi, Japanese), and a footnote definition. Asserts substrings of the concatenated block texts. This is the regression net for `markdown_speech.py`.

**`test_semantic_chunking_keeps_table_rows_and_limit`.** Long prose plus a table, `max_chars=180`. Asserts every chunk is ≤ 180 characters and at least one chunk is a table row containing `Key: One`.

**`test_large_table_cells_keep_row_context_after_a_split`.** A huge table cell forces multiple row chunks. Every such chunk must still contain `"Table 1, row 1"` — the continuation-prefix behavior.

**`test_pause_resume_survives_restart_without_regenerating_completed_chunks`.** Integration test of durability:

1. Sets `SPEAKMD_TTS_BACKEND=tone` and a 30ms delay so the worker can be observed mid-job.
2. Creates a `JobManager` on `tmp_path` (isolated output root — not the real `output/`).
3. Uploads a long Markdown string.
4. Waits until `processing` and at least one completed chunk.
5. Requests pause; waits for `paused`.
6. Records `attempts` for completed chunks.
7. `shutdown()` the manager (simulates process stop).
8. Constructs a **new** `JobManager` on the same directory (simulates restart).
9. `resume`, wait for `completed`.
10. Asserts final WAV exists; MP3 exists if `ffmpeg` is on PATH; completed chunks' `attempts` are unchanged (they were not re-synthesized).

`wait_for` polls `manager.get` every 20ms up to 8 seconds. That is necessary because the worker is asynchronous relative to the test thread.

Both managers are shut down in `finally` so the daemon thread does not leak into other tests.

### What is not tested

- HTTP routes, multipart upload, 400/404 mapping.
- Kokoro itself (intentionally; tone backend avoids model assets).
- `ResourceMonitor` / `nvidia-smi`.
- CUDA device selection failures.
- Cancel discarding in-flight audio.
- `valid_wav` of truncated files.
- Duplicate upload `existing: true`.
- 100 MiB limit, bad encodings, invalid voices.

`httpx` being a declared but unused dev dependency is a hint that HTTP tests were anticipated.

---

## 11. Configuration and environment

There is no `config.py`, no `.env` file, and no Pydantic `BaseSettings`. Configuration is:

1. Constants in code (`DEFAULT_MAX_CHARS`, sample rate 24000, MP3 `64k`, chunk pause table, upload 100 MiB).
2. Per-job settings from the UI.
3. Process environment variables.

### Environment variables

| Variable | Where read | Default | Purpose |
| --- | --- | --- | --- |
| `SPEAKMD_HOST` | `app.main()` | `127.0.0.1` | Bind address |
| `SPEAKMD_PORT` | `app.main()` | `8000` | Bind port |
| `SPEAKMD_OUTPUT` | `app.py` import | `<repo>/output` | Job directory |
| `SPEAKMD_TTS_BACKEND` | `tts.make_renderer` | (unset → Kokoro) | Set to `tone` for tests |
| `SPEAKMD_TONE_DELAY` | `ToneRenderer` | `"0"` | Seconds to sleep per fake synth |

`SPEAKMD_OUTPUT` is expanded with `expanduser()` (`~` works) and resolved to an absolute path.

`SPEAKMD_HOST=0.0.0.0` exposes the unauthenticated app on all interfaces. The README says to do this only on a trusted local network.

### Per-job settings (not env)

Stored in `job.json` and included in the settings hash: `voice`, `speed`, `device`, `max_chars`, `chunking_version`.

### External services

| Service | When | Failure mode |
| --- | --- | --- |
| Hugging Face Hub (model download) | First Kokoro pipeline creation | `synthesize` raises; job chunk fails |
| `ffmpeg` | Final MP3 | Warning; WAV still completes |
| `nvidia-smi` | Metrics poll | GPU tile "unavailable" |
| PyTorch CUDA | `device` auto/cuda | auto → CPU; cuda requested → `RuntimeError` |

No API keys are read. There is no database URL.

### Runtime package layout inside `.venv`

After `uv sync`, `.venv` contains a Python 3.11/3.12 interpreter and `site-packages` with FastAPI, Kokoro, etc. `uv run` prepends that environment. You do not `source .venv/bin/activate` unless you want to; `uv run` is sufficient (like `npx` always using local bins).

---

## 12. Python best practices

This section separates **what the code does**, **what Python convention usually recommends**, and **gaps**. Concrete change ideas are in [§13](#13-application-specific-recommendations).

### Project structure and packaging

**Done well.** Src layout, `pyproject.toml` only (no legacy `setup.py`), lockfile, tests outside the package, console script entry, Python version pinned, package-data static HTML living next to the code that serves it.

**Convention.** This matches current packaging guidance (PyPA src layout, PEP 621). Hatchling as build backend is a standard choice.

### Naming (PEP 8)

Python convention: `snake_case` functions and variables, `PascalCase` classes, `UPPER_SNAKE` constants, leading underscore for internal APIs.

The application follows this (`plan_chunks`, `JobManager`, `DEFAULT_MAX_CHARS`, `_prepare`). JSON keys are `snake_case` too (`max_chars`, `render_seconds`), which is Pythonic; a typical Node API might have used camelCase. The UI uses the same snake_case field names.

### Type hints

**Present** on public functions, dataclasses, and many internals. `from __future__ import annotations` is consistent.

**Gaps vs strict Python typing practice.** `dict[str, Any]` for jobs instead of a TypedDict or Pydantic model; `_renderer` typed implicitly as `None` then untyped; `ResourceMonitor._gpu_cache: dict` without parameters; `chunks_as_dicts` returns `list[dict]`; no mypy/pyright config. FastAPI would happily validate settings with a Pydantic model; validation is handwritten in `_validate_settings` instead.

Hand validation is not "wrong"; it is less self-documenting than a schema class.

### Error handling

**Done well.** Specific exceptions (`JSONDecodeError`, `FileNotFoundError`, `wave.Error`), `raise ... from exc`, HTTP mapping at the boundary, worker never dies on one bad job, ffmpeg failures become warnings.

**Convention.** Avoid bare `except:` (the worker uses `except Exception`, which is the accepted last-resort form). Do not swallow errors silently — the inner `except Exception: pass` when even saving failure state fails is a rare but real swallow.

### Logging

**The application does not use the `logging` module.** Progress is stored in `job["stage"]` and shown in the UI. Worker failures become `job["error"]` strings.

Python convention for services is `logging.getLogger(__name__)`. For a local UI-driven tool, job-state-as-log is understandable. For diagnosing Kokoro/CUDA issues, stdout logs would help; they are not there.

### Dependency management

**Done well.** Locked uv graph, extras for dev, version upper bounds (`fastapi>=0.115,<1`) to avoid accidental major upgrades.

**Note.** NumPy and torch are implicit. If Kokoro ever dropped NumPy as a dependency, `import numpy` inside `audio.py` would fail. Pinning `numpy` explicitly is a common practice when you import it yourself.

### Configuration

Env vars with defaults match 12-factor instincts. Import-time `OUTPUT_ROOT` is a sharp edge (must be set before import). No `.env` loader is fine for a README-driven local tool.

### Security

**Aligned with the stated threat model** (one trusted local user on loopback):

- Job IDs must match `SAFE_ID` (no `../` in paths).
- Bind default 127.0.0.1.
- Upload size cap.
- Voice/device/max_chars validated.

**Not present (and not claimed):** authentication, CSRF tokens, rate limiting, sanitizing Markdown beyond narration, checking that `/files` cannot escape `OUTPUT_ROOT` (Starlette's `StaticFiles` is designed not to; job ids are still guessable if someone can hit the port). Serving `/docs` OpenAPI on a LAN bind would describe the API to anyone who can connect.

### Testing

**Done well.** Real durability test with restart; no network; deterministic tone renderer; `tmp_path` isolation; `finally: shutdown()`.

**Convention gaps.** No HTTP tests despite `httpx`; no parameterization (`@pytest.mark.parametrize`) for table-driven narrator cases; one 8s timeout integration test that could be flaky on a very slow machine (delay is 30ms per chunk, so usually fine).

### Separation of concerns

Clear layers: HTTP / jobs / narration / chunking / TTS / audio / monitor. The UI is decoupled by JSON. This is good Python *and* good general design.

`jobs.py` at ~500 lines is the "god object" risk; it still has a coherent single responsibility (job state machine). Splitting store vs worker would be style, not necessity.

### Dependency injection

**Current:** `JobManager(OUTPUT_ROOT)` constructed in lifespan; renderer factory reads env globally; `make_renderer` is a function, which tests hook via env, not via passing a renderer into `JobManager`.

**Python practice.** FastAPI `Depends()`, or passing a `renderer_factory` into `JobManager.__init__`, would make tests slightly cleaner than mutating env. The env switch is still a valid, simple seam (`SPEAKMD_TTS_BACKEND`).

### Async programming

**Current:** async HTTP, sync worker thread. Correct for this workload.

**Pitfall to avoid if you extend the app:** do not call `renderer.synthesize` directly inside an `async def` route. That would block the event loop. Keep heavy work on the worker thread (or `asyncio.to_thread` if you refactor).

### Resource management

**Done well.** `with` for files and WAV handles; atomic temp files; `fsync` before replace; lifespan shutdown; `missing_ok` cleanup.

**Gap.** `shutdown` joins the worker with `timeout=3` seconds and does not interrupt in-flight Kokoro. A WAV being written might still finish via the daemon thread, or the process may exit first. Daemon threads are killed at interpreter shutdown — the last chunk can be lost, which the design already accepts.

### API design

JSON `detail` errors, REST-ish job resources, POST for verbs (pause/resume) rather than PATCH, polling. Consistent with small FastAPI apps. No pagination on `GET /api/jobs`. No ETag / conditional requests.

### Documentation

README is strong for users. Code docstrings on modules and several public functions are genuine (not restating the function name). This `docs/` guide is the developer onboarding document.

---

## 13. Application-specific recommendations

These are optional improvements evaluated against Python practice and *this* codebase's goals. They are not implied bugs unless stated.

### High value, small surface

1. **Add HTTP tests with FastAPI `TestClient` + httpx** for upload validation, 404s, and "existing job" reuse. The dependency is already declared.

2. **Declare `numpy` in `pyproject.toml` dependencies** because the application imports it directly.

3. **Move `import numpy as np` in `jobs.py` out of the per-chunk loop** to the top of `_process` or the module. Importing every chunk is cached by Python after the first time, so this is readability more than performance, but it looks accidental.

4. **Use `logging` for worker exceptions** in addition to `job["error"]`, especially the last-resort `except Exception` path and the swallowed save failure.

5. **A TypedDict or Pydantic model for `job.json`** would document the schema (`JOB_SCHEMA_VERSION = 1` already anticipates evolution) and catch key typos.

### Product / robustness

6. **`text_sha256` is unused at runtime.** Either use it in `_reconcile_checkpoints` to invalidate a WAV if narration text changed in place, or stop storing it. Today a new job id is the invalidation mechanism.

7. **Shutdown can drop an in-flight chunk** (daemon thread, 3s join). If graceful stop matters, wait longer when `_active_job` is set, or set a control flag and join without a tight timeout.

8. **OpenAPI at `/docs` is enabled by default.** For a loopback tool this is convenient. If `SPEAKMD_HOST=0.0.0.0` is used, consider disabling docs or adding a warning.

9. **`GET /api/jobs` loads every `job.json`.** After many large documents (each JSON includes all chunk texts on disk; summaries drop texts but still parse the full file), this could get slow. An index or omitting `chunks` at load would help; `JobStore.load` always `json.load`s the whole file.

10. **UI pause is disabled during `parsing_markdown`**, though the API supports it. Either enable the button or document that parse is not pausable from the UI.

### Typing and tooling (optional)

11. Add Ruff (linter/formatter, the Python ecosystem's current default, analogous to ESLint + Prettier) and optionally Pyright/mypy. None are configured today.

12. Pin a `.python-version` or document `uv python pin 3.12` so the 3.11–3.12 window is explicit in the checkout.

### Out of scope unless requirements change

- Authentication, multi-user queues, Redis, Docker: the architecture would change, not just wrap.
- Replacing the worker thread with FastAPI `BackgroundTasks` would lose the "one model" guarantee unless you still serialize work.
- Expanding voices/languages is README-listed future work; backend already accepts any well-formed Kokoro voice name.

---

## 14. Glossary of Python concepts

Terms as they appear in this project.

| Term | Meaning here | TypeScript / Node analogue |
| --- | --- | --- |
| **ASGI** | Protocol between Uvicorn and FastAPI | "What connects Express to Node's HTTP server," standardized |
| **async context manager** | `async with` / `@asynccontextmanager` + `yield` | `AsyncDisposable` / try/finally around awaitable setup |
| **bytecode / `__pycache__`** | `.pyc` compiled files | Not user-facing in Node; closer to `.ts` → `.js` emit, but automatic |
| **console script** | `speakmd = "speakmd.app:main"` | `package.json` `"bin"` |
| **dataclass** | Class with generated init/eq, optional frozen | Interface + constructor; or a small Zod object |
| **decorator** | `@app.get`, `@dataclass` | Decorators / higher-order functions wrapping a class or function |
| **Ellipsis `...`** | Used in `File(...)` as FastAPI "required" | No direct equivalent; a sentinel value |
| **f-string** | `f"Chunk {index}"` | Template literal |
| **fixture** | pytest injects `tmp_path`, `monkeypatch` by parameter name | Jest setup + helper args, but automatic |
| **frozen** | Immutable dataclass | `readonly` + runtime throw on assign |
| **GIL** | One bytecode thread at a time in CPython | No equivalent; Node is single-threaded JS plus worker isolates |
| **hatchling** | Build backend that produces wheels | `tsc` + packing, or the "build" field in package.json |
| **hint / annotation** | `x: str` not enforced at runtime | TypeScript types (erased at compile) — similar erasure, but TS is checked by default in a TS project |
| **lockfile** | `uv.lock` | `package-lock.json` |
| **module** | One `.py` file | One ESM file |
| **package** | Directory with `__init__.py` | An npm package, or a folder with `index.ts` |
| **`Path`** | Object-oriented filesystem path | `path` + `fs` together |
| **pyproject.toml** | Project manifest | `package.json` (+ parts of tsconfig/jest config) |
| **pytest** | Test runner | Jest / Vitest / node:test |
| **`queue.Queue`** | Thread-safe in-memory queue | Not BullMQ; closer to an array + mutex |
| **`RLock`** | Reentrant mutex | Mutex that the same thread can acquire twice |
| **site-packages** | Where installed libraries live in `.venv` | `node_modules` |
| **src layout** | Package under `src/` | `src/` in a TS repo, plus installing the package |
| **stdlib** | Built-in library (`json`, `wave`, `pathlib`) | Node core modules |
| **type ignore / Any** | Escape hatch | `any` |
| **uv** | Package/project manager | npm + nvm-ish + lock solver |
| **virtualenv (`.venv`)** | Isolated interpreter + libraries | `node_modules` + a pinned Node via nvm |
| **wheel** | Installable binary/library archive | An npm tarball, roughly |
| **`with`** | Deterministic setup/teardown | `try/finally` or `using` |
| **walrus `:=`** | Assign in an expression | `while ((x = f())) { }` |

---

## 15. Further reading

Tied to what this repo actually uses.

### This application

- [README.md](../README.md) — user-facing install, UX, limitations.
- FastAPI automatic docs when the server is running: `http://127.0.0.1:8000/docs` (Swagger UI) and `/redoc`. The routes in `app.py` are the contract.

### Python language and style

- [PEP 8](https://peps.python.org/pep-0008/) — style guide (snake_case, imports, line length).
- [PEP 621](https://peps.python.org/pep-0621/) — `pyproject.toml` `[project]` table.
- [Dataclasses](https://docs.python.org/3/library/dataclasses.html)
- [pathlib](https://docs.python.org/3/library/pathlib.html)
- [typing](https://docs.python.org/3/library/typing.html) — `Literal`, `Any`, `Iterable`
- [asyncio (conceptual)](https://docs.python.org/3/library/asyncio.html) — even though the worker is threaded

### Tooling

- [uv documentation](https://docs.astral.sh/uv/) — `sync`, `run`, lockfiles, extras.
- [pytest fixtures](https://docs.pytest.org/en/stable/explanation/fixtures.html) — why `tmp_path` works by magic.
- [Hatchling](https://hatch.pypa.io/latest/) — build backend used here.

### Libraries

- [FastAPI](https://fastapi.tiangolo.com/) — routing, uploads, lifespan, static files.
- [Uvicorn](https://www.uvicorn.org/)
- [markdown-it-py](https://markdown-it-py.readthedocs.io/) — especially if you already know JS `markdown-it`.
- [Kokoro](https://github.com/hexgrad/kokoro) / model card `hexgrad/Kokoro-82M` on Hugging Face — voice names and language codes.

### Mental model bridges

If you want one sentence for each major shift:

- **Package management:** `uv sync` is `npm install`; `.venv` is an isolated Node plus `node_modules`.
- **Web:** FastAPI + Uvicorn is Express/Nest + the HTTP server, with type hints playing a similar role to DTO validation *when you use Pydantic*.
- **Jobs:** `JobManager` is an in-process worker with a directory-backed store, not Celery and not BullMQ.
- **Types:** hints are documentation and optional checking, not a compiler.
- **Concurrency:** one asyncio loop for HTTP, one thread for TTS, GIL released inside NumPy/PyTorch.

That is the whole application: a small FastAPI process, a careful filesystem state machine, and a local speech model, written in idiomatic Python that a TypeScript developer can map onto without pretending the two ecosystems are the same.
