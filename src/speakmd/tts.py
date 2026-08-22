"""Kokoro renderer with lazy hardware discovery and model reuse."""

from __future__ import annotations

import os
from typing import Literal


Device = Literal["auto", "cpu", "cuda"]


def choose_device(requested: str = "auto") -> str:
    if requested not in {"auto", "cpu", "cuda"}:
        raise ValueError("device must be auto, cpu, or cuda")
    if requested == "cpu":
        return "cpu"
    try:
        import torch

        has_cuda = bool(torch.cuda.is_available())
    except Exception:
        has_cuda = False
    if requested == "cuda" and not has_cuda:
        raise RuntimeError("CUDA was requested but PyTorch cannot see an NVIDIA GPU")
    return "cuda" if has_cuda else "cpu"


class KokoroRenderer:
    """One renderer is shared by the single worker; models are never reloaded per chunk."""

    sample_rate = 24000

    def __init__(self, device: str = "auto") -> None:
        self.device = choose_device(device)
        self._pipelines: dict[str, object] = {}

    @staticmethod
    def language_for_voice(voice: str) -> str:
        # Kokoro voice names begin with the KPipeline language code.
        return voice[:1] if voice[:1] in {"a", "b", "e", "f", "h", "i", "j", "p", "z"} else "a"

    def _pipeline(self, language: str):
        pipeline = self._pipelines.get(language)
        if pipeline is None:
            try:
                from kokoro import KPipeline
            except ImportError as exc:
                raise RuntimeError(
                    "Kokoro is not installed. Run `uv sync` (and install a supported Python version)."
                ) from exc
            pipeline = KPipeline(
                lang_code=language,
                repo_id="hexgrad/Kokoro-82M",
                device=self.device,
            )
            self._pipelines[language] = pipeline
        return pipeline

    def synthesize(self, text: str, voice: str, speed: float):
        """Return a float32 mono waveform. KPipeline may further split by phoneme limit."""
        import numpy as np

        if not text.strip():
            raise ValueError("cannot synthesize empty text")
        pipeline = self._pipeline(self.language_for_voice(voice))
        pieces = []
        for result in pipeline(text, voice=voice, speed=speed):
            # Current Kokoro Result wraps a KModel.Output object; `result.audio`
            # is the actual tensor. Older releases yielded a tuple directly.
            audio = getattr(result, "audio", None)
            if audio is None and isinstance(result, tuple) and len(result) >= 3:
                audio = result[2]
            if audio is None:
                continue
            if hasattr(audio, "detach"):
                audio = audio.detach().cpu().numpy()
            waveform = np.asarray(audio, dtype=np.float32).squeeze()
            if waveform.size:
                if pieces:
                    pieces.append(np.zeros(int(self.sample_rate * 0.07), dtype=np.float32))
                pieces.append(waveform)
        if not pieces:
            raise RuntimeError("Kokoro returned no audio for this chunk")
        return np.concatenate(pieces)


class ToneRenderer:
    """A deterministic test-only renderer, selected with SPEAKMD_TTS_BACKEND=tone."""

    sample_rate = 24000
    device = "test-tone"

    def synthesize(self, text: str, voice: str, speed: float):
        import numpy as np
        import time

        delay = float(os.environ.get("SPEAKMD_TONE_DELAY", "0"))
        if delay:
            time.sleep(delay)
        seconds = max(0.08, min(1.0, len(text) / 500)) / max(speed, 0.1)
        frames = int(self.sample_rate * seconds)
        clock = np.arange(frames, dtype=np.float32) / self.sample_rate
        return 0.08 * np.sin(2 * np.pi * 220 * clock)


def make_renderer(device: str):
    if os.environ.get("SPEAKMD_TTS_BACKEND") == "tone":
        return ToneRenderer()
    return KokoroRenderer(device)
