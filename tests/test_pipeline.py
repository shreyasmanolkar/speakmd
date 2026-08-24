from __future__ import annotations

from pathlib import Path
import shutil
import time

import pytest

from speakmd.chunking import plan_chunks
from speakmd.jobs import JobManager
from speakmd.markdown_speech import narrate_markdown
from speakmd.preview import PREVIEW_MAX_CHARS, sample_catalog


def wait_for(manager: JobManager, job_id: str, predicate, timeout: float = 8):
    deadline = time.monotonic() + timeout
    latest = manager.get(job_id)
    while time.monotonic() < deadline:
        latest = manager.get(job_id)
        if predicate(latest):
            return latest
        time.sleep(0.02)
    raise AssertionError(f"Timed out; last job state: {latest}")


def test_narrator_retains_common_markdown_content():
    markdown = """---
title: Example
---

# Important *heading*

Use **bold text**, `value = 3`, and [the docs](https://example.com/a-b).

> A useful quotation.

1. First item
2. Second item

- [x] Completed task
- [ ] Incomplete task

![A diagram](images/diagram.png)

```python
def hello(name):
    return f"Hello {name}"
```

| Name | Age | Location |
| --- | ---: | --- |
| John | 30 | London |
| Sarah | 28 | Paris |

Deleted ~~old wording~~ remains visible.

Unicode remains intact: café — नमस्ते — 東京.

[^1]: A footnote definition.
"""
    narrated = narrate_markdown(markdown)
    text = "\n".join(block.text for block in narrated.blocks)
    assert "Heading level 1: Important heading" in text
    assert "link target example dot com slash a dash b" in text
    assert "inline code" in text
    assert "Quotation: A useful quotation" in text
    assert "Item 1: First item" in text
    assert "completed task Completed task" in text
    assert "incomplete task Incomplete task" in text
    assert "Image: A diagram, source images slash diagram dot png" in text
    assert "Code block in python" in text
    assert "Table 1 has 3 columns: Name, Age, Location" in text
    assert "Table 1, row 2: Name: Sarah. Age: 28. Location: Paris" in text
    assert "text marked deleted: old wording" in text
    assert "Unicode remains intact: café — नमस्ते — 東京" in text


def test_semantic_chunking_keeps_table_rows_and_limit():
    blocks = narrate_markdown(
        "# Report\n\n" + "A complete sentence. " * 40 + "\n\n| Key | Value |\n|---|---|\n| One | Two |"
    ).blocks
    chunks = plan_chunks(blocks, max_chars=180)
    assert len(chunks) > 2
    assert all(len(chunk.text) <= 180 for chunk in chunks)
    assert any("table_row" in chunk.block_kinds and "Key: One" in chunk.text for chunk in chunks)


def test_large_table_cells_keep_row_context_after_a_split():
    blocks = narrate_markdown(
        "| Name | Notes |\n|---|---|\n| Ada | " + "A detailed value with several words. " * 30 + "|"
    ).blocks
    chunks = plan_chunks(blocks, max_chars=180)
    row_chunks = [chunk for chunk in chunks if "table_row" in chunk.block_kinds]
    assert len(row_chunks) > 1
    assert all(len(chunk.text) <= 180 for chunk in row_chunks)
    assert all("Table 1, row 1" in chunk.text for chunk in row_chunks)


def test_pause_resume_survives_restart_without_regenerating_completed_chunks(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SPEAKMD_TTS_BACKEND", "tone")
    monkeypatch.setenv("SPEAKMD_TONE_DELAY", "0.03")
    markdown = ("# Long report\n\n" + "This is a complete sentence with enough words to narrate naturally. " * 5 + "\n\n") * 15
    manager = JobManager(tmp_path)
    try:
        job, _ = manager.create_job("long-report.md", markdown.encode(), {"max_chars": 180})
        job_id = job["id"]
        wait_for(manager, job_id, lambda item: item["state"] == "processing" and item["progress"]["completed"] >= 1)
        manager.request_pause(job_id)
        paused = wait_for(manager, job_id, lambda item: item["state"] == "paused")
        done_before = {
            number: state["attempts"]
            for number, state in paused["chunk_states"].items()
            if state["status"] == "completed"
        }
        assert done_before
    finally:
        manager.shutdown()

    recovered = JobManager(tmp_path)
    try:
        recovered.resume(job_id)
        complete = wait_for(recovered, job_id, lambda item: item["state"] == "completed")
        assert complete["progress"]["completed"] == complete["progress"]["total"]
        assert (tmp_path / complete["output"]["wav"]).exists()
        if shutil.which("ffmpeg"):
            assert complete["output"]["mp3"]
            assert (tmp_path / complete["output"]["mp3"]).exists()
        for number, attempts in done_before.items():
            assert complete["chunk_states"][number]["attempts"] == attempts
    finally:
        recovered.shutdown()


def test_sample_catalog_fits_preview_limit():
    catalog = sample_catalog()
    assert catalog["default_id"] == "narration"
    assert {sample["id"] for sample in catalog["samples"]} == {
        "narration",
        "conversation",
        "numbers",
        "instruction",
    }
    for sample in catalog["samples"]:
        assert 0 < len(sample["text"]) <= catalog["max_chars"]
        assert catalog["max_chars"] == PREVIEW_MAX_CHARS


def test_voice_preview_writes_wav_and_reuses_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SPEAKMD_TTS_BACKEND", "tone")
    manager = JobManager(tmp_path)
    try:
        first = manager.preview({"voice": "af_heart", "speed": 1.0, "device": "cpu"})
        assert first["cached"] is False
        assert first["voice"] == "af_heart"
        assert "morning light" in first["text"]
        wav = tmp_path / first["audio"]
        assert wav.exists()
        assert wav.stat().st_size > 44
        second = manager.preview({"voice": "af_heart", "speed": 1.0, "device": "cpu"})
        assert second["cached"] is True
        assert second["audio"] == first["audio"]
        conversation = manager.preview({"voice": "af_heart"}, sample_id="conversation")
        assert "Can you hear me" in conversation["text"]
        custom = manager.preview({"voice": "bf_emma"}, text="Hello from Emma.")
        assert custom["text"] == "Hello from Emma."
        assert custom["audio"] != first["audio"]
        assert (tmp_path / custom["audio"]).exists()
    finally:
        manager.shutdown()


def test_voice_preview_rejects_invalid_text(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SPEAKMD_TTS_BACKEND", "tone")
    manager = JobManager(tmp_path)
    try:
        with pytest.raises(ValueError, match="empty"):
            manager.preview({}, text="   ")
        with pytest.raises(ValueError, match="limited"):
            manager.preview({}, text="x" * (PREVIEW_MAX_CHARS + 1))
        with pytest.raises(ValueError, match="unknown sample"):
            manager.preview({}, sample_id="not-a-sample")
        with pytest.raises(ValueError, match="voice"):
            manager.preview({"voice": "not-a-voice"}, text="Hello.")
    finally:
        manager.shutdown()


def test_voice_preview_refuses_while_a_job_is_active(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SPEAKMD_TTS_BACKEND", "tone")
    monkeypatch.setenv("SPEAKMD_TONE_DELAY", "0.05")
    markdown = (
        "# Long report\n\n"
        + "This is a complete sentence with enough words to narrate naturally. " * 5
        + "\n\n"
    ) * 15
    manager = JobManager(tmp_path)
    try:
        job, _ = manager.create_job("long-report.md", markdown.encode(), {"max_chars": 180})
        wait_for(manager, job["id"], lambda item: item["state"] == "processing")
        with pytest.raises(ValueError, match="speech worker is busy"):
            manager.preview({"voice": "af_heart"}, text="Hello from a busy worker.")
    finally:
        manager.shutdown()
