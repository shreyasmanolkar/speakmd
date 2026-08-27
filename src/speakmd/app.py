"""Local FastAPI application for SpeakMD."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .jobs import JobManager
from .monitor import ResourceMonitor
from .preview import sample_catalog


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = Path(os.environ.get("SPEAKMD_OUTPUT", ROOT / "output")).expanduser().resolve()
STATIC_ROOT = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.manager = JobManager(OUTPUT_ROOT)
    app.state.monitor = ResourceMonitor()
    yield
    app.state.manager.shutdown()


OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="SpeakMD", version="0.1.0", lifespan=lifespan)
app.mount("/files", StaticFiles(directory=OUTPUT_ROOT), name="files")


def manager() -> JobManager:
    return app.state.manager


def api_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_ROOT / "index.html")


@app.get("/learn", include_in_schema=False)
async def learning_guide() -> FileResponse:
    return FileResponse(STATIC_ROOT / "learning.html")


@app.post("/api/jobs")
async def upload_markdown(file: UploadFile = File(...), settings: str = Form("{}")):
    filename = file.filename or "document.md"
    if Path(filename).suffix.lower() not in {".md", ".markdown", ".mdown", ".mkdn"}:
        raise HTTPException(status_code=400, detail="Please upload a Markdown file (.md or .markdown).")
    try:
        parsed_settings = json.loads(settings)
        if not isinstance(parsed_settings, dict):
            raise ValueError("settings must be an object")
        job, existing = manager().create_job(filename, await file.read(), parsed_settings)
        return {"job": job, "existing": existing}
    except (ValueError, json.JSONDecodeError) as exc:
        raise api_error(exc) from exc


@app.get("/api/jobs")
async def list_jobs():
    return {"jobs": manager().list(), "queue": manager().queue_status()}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    try:
        return manager().get(job_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@app.post("/api/jobs/{job_id}/pause")
async def pause_job(job_id: str):
    try:
        return manager().request_pause(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except ValueError as exc:
        raise api_error(exc) from exc


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str):
    try:
        return manager().resume(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except ValueError as exc:
        raise api_error(exc) from exc


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    try:
        return manager().cancel(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str):
    try:
        return manager().retry_failed(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except ValueError as exc:
        raise api_error(exc) from exc


@app.get("/api/preview")
async def preview_samples():
    return sample_catalog()


@app.post("/api/preview")
async def create_preview(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="preview settings must be an object")
    try:
        return await asyncio.to_thread(
            manager().preview,
            payload,
            payload.get("text"),
            payload.get("sample_id"),
        )
    except ValueError as exc:
        raise api_error(exc) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/metrics")
async def metrics():
    snapshot = app.state.monitor.snapshot()
    snapshot["tts"] = manager().queue_status()
    return snapshot


def main() -> None:
    import uvicorn

    uvicorn.run(
        "speakmd.app:app",
        host=os.environ.get("SPEAKMD_HOST", "127.0.0.1"),
        port=int(os.environ.get("SPEAKMD_PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    main()
