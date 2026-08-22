"""Durable, single-worker job processing for SpeakMD.

The job JSON is deliberately the source of truth.  It is atomically replaced after
every state transition, while every chunk WAV is atomically written first.  On a
restart a valid WAV whose state update was interrupted is safely recovered.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
from typing import Any

from .audio import combine_wav_chunks, encode_mp3, valid_wav, write_wav_atomic
from .chunking import CHUNKING_VERSION, DEFAULT_MAX_CHARS, chunks_as_dicts, plan_chunks
from .markdown_speech import narrate_markdown
from .tts import make_renderer


JOB_SCHEMA_VERSION = 1
ACTIVE_STATES = {"queued", "preparing", "parsing_markdown", "processing", "resuming"}
TERMINAL_STATES = {"completed", "cancelled", "failed"}
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,100}$")


def now() -> str:
    return datetime.now(UTC).isoformat()


def slugify(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return value[:48] or "document"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part-{os.getpid()}")
    with open(temporary, "w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path(self, job_id: str) -> Path:
        if not SAFE_ID.fullmatch(job_id):
            raise ValueError("invalid job id")
        return self.root / job_id

    def state_path(self, job_id: str) -> Path:
        return self.path(job_id) / "job.json"

    def load(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            with open(self.state_path(job_id), encoding="utf-8") as source:
                return json.load(source)

    def save(self, job: dict[str, Any]) -> None:
        with self._lock:
            job["updated_at"] = now()
            atomic_json(self.state_path(job["id"]), job)

    def ids(self) -> list[str]:
        return sorted(
            directory.name
            for directory in self.root.iterdir()
            if directory.is_dir() and SAFE_ID.fullmatch(directory.name) and (directory / "job.json").exists()
        )


class JobManager:
    """A deliberately small persistent queue: one model, one renderer, one job at a time."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root).resolve()
        self.store = JobStore(self.output_root)
        self._queue: queue.Queue[str] = queue.Queue()
        self._enqueued: set[str] = set()
        self._controls: dict[str, str] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._active_job: str | None = None
        self._renderer = None
        self._renderer_device: str | None = None
        self._worker = threading.Thread(target=self._work, name="speakmd-worker", daemon=True)
        self._recover_jobs()
        self._worker.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._queue.put("")
        self._worker.join(timeout=3)

    def _recover_jobs(self) -> None:
        """Requeue interrupted work; preserved checkpoints are reconciled by the worker."""
        for job_id in self.store.ids():
            job = self.store.load(job_id)
            if job.get("state") in {"preparing", "parsing_markdown", "processing", "resuming"}:
                job["state"] = "queued"
                job["stage"] = "Recovered after application restart"
                job["recovered_at"] = now()
                self.store.save(job)
            if job.get("state") == "queued":
                self._enqueue(job_id)

    def _enqueue(self, job_id: str) -> None:
        with self._lock:
            if job_id not in self._enqueued:
                self._enqueued.add(job_id)
                self._queue.put(job_id)

    def create_job(self, filename: str, raw_markdown: bytes, settings: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Persist the upload immediately. Parsing happens in the worker, not the request."""
        if len(raw_markdown) > 100 * 1024 * 1024:
            raise ValueError("Markdown uploads are limited to 100 MiB")
        try:
            raw_markdown.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Markdown must be UTF-8 encoded") from exc
        clean_settings = self._validate_settings(settings)
        source_hash = sha256_bytes(raw_markdown)
        settings_hash = sha256_bytes(json.dumps(clean_settings, sort_keys=True).encode())
        stem = slugify(Path(filename or "document.md").stem)
        job_id = f"{stem}-{source_hash[:12]}-{settings_hash[:8]}"
        job_dir = self.store.path(job_id)
        with self._lock:
            if self.store.state_path(job_id).exists():
                job = self.store.load(job_id)
                if job["state"] == "queued":
                    self._enqueue(job_id)
                return self.summary(job), True
            (job_dir / "chunks").mkdir(parents=True, exist_ok=True)
            (job_dir / "final").mkdir(parents=True, exist_ok=True)
            source_path = job_dir / "input.md"
            temporary = source_path.with_name(f".{source_path.name}.part-{os.getpid()}")
            with open(temporary, "wb") as output:
                output.write(raw_markdown)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, source_path)
            job = {
                "schema_version": JOB_SCHEMA_VERSION,
                "id": job_id,
                "document_name": Path(filename or "document.md").name,
                "output_name": stem,
                "source_sha256": source_hash,
                "settings": clean_settings,
                "created_at": now(),
                "updated_at": now(),
                "state": "queued",
                "stage": "Waiting in local queue",
                "warnings": [],
                "error": None,
                "chunks": [],
                "chunk_states": {},
                "progress": {"total": 0, "completed": 0, "failed": 0, "current": 0, "render_seconds": 0.0},
                "output": {"wav": None, "mp3": None},
            }
            self.store.save(job)
            self._enqueue(job_id)
            return self.summary(job), False

    @staticmethod
    def _validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
        voice = str(settings.get("voice") or "af_heart").strip()
        if not re.fullmatch(r"[a-z]{2}_[a-z0-9_]+", voice):
            raise ValueError("voice must be a Kokoro voice name, for example af_heart")
        try:
            speed = float(settings.get("speed", 1.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("speed must be a number") from exc
        if not 0.5 <= speed <= 2.0:
            raise ValueError("speed must be between 0.5 and 2.0")
        device = str(settings.get("device") or "auto")
        if device not in {"auto", "cpu", "cuda"}:
            raise ValueError("device must be auto, cpu, or cuda")
        try:
            max_chars = int(settings.get("max_chars", DEFAULT_MAX_CHARS))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_chars must be an integer") from exc
        if not 160 <= max_chars <= 450:
            raise ValueError("max_chars must be between 160 and 450")
        return {
            "voice": voice,
            "speed": speed,
            "device": device,
            "max_chars": max_chars,
            "chunking_version": CHUNKING_VERSION,
        }

    def get(self, job_id: str) -> dict[str, Any]:
        return self.summary(self.store.load(job_id))

    def list(self) -> list[dict[str, Any]]:
        jobs = [self.summary(self.store.load(job_id)) for job_id in self.store.ids()]
        return sorted(jobs, key=lambda job: job["updated_at"], reverse=True)

    def summary(self, job: dict[str, Any]) -> dict[str, Any]:
        """Avoid sending every chunk's full text over polling requests."""
        value = deepcopy(job)
        chunks = value.pop("chunks", [])
        current = int(value.get("progress", {}).get("current", 0))
        if current and current <= len(chunks):
            value["current_chunk_text"] = chunks[current - 1]["text"][:260]
        else:
            value["current_chunk_text"] = None
        value["queue"] = self.queue_status()
        value["progress"] = dict(value.get("progress", {}))
        total = int(value["progress"].get("total", 0))
        completed = int(value["progress"].get("completed", 0))
        render_seconds = float(value["progress"].get("render_seconds", 0))
        value["progress"]["percentage"] = round(100 * completed / total, 1) if total else 0.0
        value["progress"]["eta_seconds"] = (
            round(render_seconds / completed * (total - completed), 1) if completed else None
        )
        return value

    def queue_status(self) -> dict[str, Any]:
        with self._lock:
            return {"active_job": self._active_job, "waiting": self._queue.qsize()}

    def request_pause(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.store.load(job_id)
            if job["state"] in TERMINAL_STATES:
                raise ValueError(f"cannot pause a {job['state']} job")
            if job["state"] in {"queued", "preparing", "parsing_markdown", "resuming"}:
                job["state"] = "paused"
                job["stage"] = "Paused before rendering"
                self.store.save(job)
            else:
                self._controls[job_id] = "pause"
                job["stage"] = "Pause requested; finishing the current TTS call"
                self.store.save(job)
            return self.summary(job)

    def resume(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.store.load(job_id)
            if job["state"] != "paused":
                raise ValueError("only paused jobs can be resumed")
            self._controls.pop(job_id, None)
            job["state"] = "resuming"
            job["stage"] = "Queued to resume from completed checkpoints"
            self.store.save(job)
            self._enqueue(job_id)
            return self.summary(job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.store.load(job_id)
            if job["state"] in TERMINAL_STATES:
                return self.summary(job)
            if job["state"] in {"queued", "paused", "preparing", "parsing_markdown", "resuming"}:
                job["state"] = "cancelled"
                job["stage"] = "Cancelled"
                self.store.save(job)
            else:
                self._controls[job_id] = "cancel"
                job["stage"] = "Cancellation requested; finishing the current TTS call"
                self.store.save(job)
            return self.summary(job)

    def retry_failed(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.store.load(job_id)
            failed = [state for state in job.get("chunk_states", {}).values() if state["status"] == "failed"]
            for state in failed:
                state["status"] = "pending"
                state["error"] = None
            if not failed and job.get("state") != "failed":
                raise ValueError("this job has no failed chunks")
            job["state"] = "queued"
            job["stage"] = "Retrying failed chunks" if failed else "Retrying final assembly or document preparation"
            job["error"] = None
            self._refresh_progress(job)
            self.store.save(job)
            self._enqueue(job_id)
            return self.summary(job)

    def _control(self, job_id: str) -> str | None:
        with self._lock:
            return self._controls.get(job_id)

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not job_id:
                continue
            with self._lock:
                self._enqueued.discard(job_id)
                self._active_job = job_id
            try:
                self._process(job_id)
            except Exception as exc:  # Last-resort guard: the worker must not die.
                try:
                    job = self.store.load(job_id)
                    job["state"] = "failed"
                    job["stage"] = "Failed"
                    job["error"] = f"Unexpected worker error: {exc}"
                    self.store.save(job)
                except Exception:
                    pass
            finally:
                with self._lock:
                    self._active_job = None
                    self._controls.pop(job_id, None)
                self._queue.task_done()

    def _prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        if job.get("chunks"):
            return job
        job["state"] = "preparing"
        job["stage"] = "Reading uploaded Markdown"
        self.store.save(job)
        raw = (self.store.path(job["id"]) / "input.md").read_text(encoding="utf-8-sig")
        job["state"] = "parsing_markdown"
        job["stage"] = "Parsing Markdown and narrating tables, links, and code"
        self.store.save(job)
        narrated = narrate_markdown(raw)
        chunks = plan_chunks(narrated.blocks, job["settings"]["max_chars"])
        if not chunks:
            raise RuntimeError("This Markdown document contains no narratable content")
        job["chunks"] = chunks_as_dicts(chunks)
        job["chunk_states"] = {
            f"{chunk.index:04d}": {
                "status": "pending",
                "audio": f"chunks/{chunk.index:04d}.wav",
                "text_sha256": sha256_bytes(chunk.text.encode("utf-8")),
                "attempts": 0,
                "error": None,
                "render_seconds": None,
            }
            for chunk in chunks
        }
        job["warnings"] = narrated.warnings
        job["progress"] = {"total": len(chunks), "completed": 0, "failed": 0, "current": 0, "render_seconds": 0.0}
        self.store.save(job)
        return job

    def _renderer_for(self, device: str):
        if self._renderer is None or self._renderer_device != device:
            self._renderer = make_renderer(device)
            self._renderer_device = device
        return self._renderer

    def _reconcile_checkpoints(self, job: dict[str, Any]) -> None:
        directory = self.store.path(job["id"])
        changed = False
        for state in job["chunk_states"].values():
            audio = directory / state["audio"]
            if state["status"] == "completed" and not valid_wav(audio):
                state["status"] = "pending"
                state["error"] = "Checkpoint was missing or invalid and will be regenerated"
                changed = True
            elif state["status"] in {"pending", "processing"} and valid_wav(audio):
                # A crash may have happened after an atomic WAV write but before the JSON write.
                state["status"] = "completed"
                state["error"] = None
                changed = True
            elif state["status"] == "processing":
                state["status"] = "pending"
                changed = True
        self._refresh_progress(job)
        if changed:
            self.store.save(job)

    @staticmethod
    def _refresh_progress(job: dict[str, Any]) -> None:
        states = list(job.get("chunk_states", {}).values())
        completed = [state for state in states if state["status"] == "completed"]
        job["progress"]["completed"] = len(completed)
        job["progress"]["failed"] = sum(state["status"] == "failed" for state in states)
        job["progress"]["render_seconds"] = round(
            sum(float(state.get("render_seconds") or 0) for state in completed), 3
        )

    def _process(self, job_id: str) -> None:
        job = self.store.load(job_id)
        if job["state"] in TERMINAL_STATES or job["state"] == "paused":
            return
        job = self._prepare(job)
        # A pause/cancel may arrive while Markdown is being parsed.  Reload the
        # authoritative state before beginning any model work.
        job = self.store.load(job_id)
        if job["state"] in TERMINAL_STATES or job["state"] == "paused":
            return
        if self._control(job_id) == "cancel":
            job["state"], job["stage"] = "cancelled", "Cancelled"
            self.store.save(job)
            return
        if self._control(job_id) == "pause":
            job["state"], job["stage"] = "paused", "Paused"
            self.store.save(job)
            return
        self._reconcile_checkpoints(job)
        job["state"] = "processing"
        job["stage"] = "Loading Kokoro model when needed"
        job["error"] = None
        self.store.save(job)
        renderer = self._renderer_for(job["settings"]["device"])
        directory = self.store.path(job_id)

        for chunk in job["chunks"]:
            index = int(chunk["index"])
            state = job["chunk_states"][f"{index:04d}"]
            if state["status"] == "completed":
                continue
            control = self._control(job_id)
            if control == "cancel":
                job["state"], job["stage"] = "cancelled", "Cancelled"
                self.store.save(job)
                return
            if control == "pause":
                job["state"], job["stage"] = "paused", "Paused"
                self.store.save(job)
                return
            state["status"] = "processing"
            state["attempts"] = int(state.get("attempts", 0)) + 1
            job["progress"]["current"] = index
            job["stage"] = f"Synthesizing chunk {index} of {len(job['chunks'])}"
            self.store.save(job)
            started = time.monotonic()
            try:
                audio = renderer.synthesize(chunk["text"], job["settings"]["voice"], job["settings"]["speed"])
                # Cancellation does not retain audio generated after the request, but pause does:
                # it records the finished chunk before stopping at the next boundary.
                if self._control(job_id) == "cancel":
                    state["status"] = "pending"
                    job["state"], job["stage"] = "cancelled", "Cancelled"
                    self.store.save(job)
                    return
                import numpy as np

                pause = np.zeros(int(renderer.sample_rate * float(chunk["pause_after"])), dtype=np.float32)
                write_wav_atomic(directory / state["audio"], np.concatenate([audio, pause]), renderer.sample_rate)
                state["status"] = "completed"
                state["error"] = None
                state["render_seconds"] = round(time.monotonic() - started, 3)
                self._refresh_progress(job)
                if self._control(job_id) == "pause":
                    job["state"], job["stage"] = "paused", "Paused after a completed chunk"
                    self.store.save(job)
                    return
                self.store.save(job)
            except Exception as exc:
                state["status"] = "failed"
                state["error"] = str(exc)
                self._refresh_progress(job)
                job["state"] = "failed"
                job["stage"] = f"Chunk {index} failed; retry is available"
                job["error"] = f"Chunk {index}: {exc}"
                self.store.save(job)
                return

        control = self._control(job_id)
        if control == "cancel":
            job["state"], job["stage"] = "cancelled", "Cancelled"
            self.store.save(job)
            return
        if control == "pause":
            job["state"], job["stage"] = "paused", "Paused before final assembly"
            self.store.save(job)
            return
        job["stage"] = "Combining completed WAV checkpoints"
        self.store.save(job)
        paths = [directory / job["chunk_states"][f"{int(chunk['index']):04d}"]["audio"] for chunk in job["chunks"]]
        final_dir = directory / "final"
        final_wav = final_dir / f"{job['output_name']}.wav"
        final_mp3 = final_dir / f"{job['output_name']}.mp3"
        combine_wav_chunks(paths, final_wav)
        warning = encode_mp3(final_wav, final_mp3)
        job["output"] = {
            "wav": str(final_wav.relative_to(self.output_root)),
            "mp3": str(final_mp3.relative_to(self.output_root)) if final_mp3.exists() else None,
        }
        if warning:
            job["warnings"] = list(dict.fromkeys([*job.get("warnings", []), warning]))
        job["state"] = "completed"
        job["stage"] = "Completed"
        job["progress"]["current"] = len(job["chunks"])
        self.store.save(job)
