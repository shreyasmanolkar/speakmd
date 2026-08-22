"""Small, streaming-safe audio helpers.

WAV is used for chunk checkpoints: it is easy to validate and concatenate without
decoding.  A final MP3 is an optional convenience copy made by ffmpeg.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import wave


def write_wav_atomic(path: Path, samples, sample_rate: int) -> None:
    """Atomically write mono signed-16-bit PCM WAV without holding a whole document."""
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.part-{os.getpid()}.wav")
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2")
    try:
        with wave.open(str(tmp), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(pcm.tobytes())
        with open(tmp, "rb") as written:
            os.fsync(written.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def valid_wav(path: Path, expected_rate: int | None = None) -> bool:
    try:
        with wave.open(str(path), "rb") as source:
            return (
                source.getnchannels() == 1
                and source.getsampwidth() == 2
                and source.getnframes() > 0
                and (expected_rate is None or source.getframerate() == expected_rate)
            )
    except (EOFError, wave.Error, OSError):
        return False


def combine_wav_chunks(chunk_paths: list[Path], final_wav: Path) -> int:
    """Copy PCM frames into one final WAV; memory remains bounded by a small buffer."""
    if not chunk_paths:
        raise ValueError("cannot combine zero chunks")
    final_wav.parent.mkdir(parents=True, exist_ok=True)
    tmp = final_wav.with_name(f".{final_wav.stem}.part-{os.getpid()}.wav")
    try:
        with wave.open(str(chunk_paths[0]), "rb") as first:
            sample_rate = first.getframerate()
            channels = first.getnchannels()
            width = first.getsampwidth()
        with wave.open(str(tmp), "wb") as output:
            output.setnchannels(channels)
            output.setsampwidth(width)
            output.setframerate(sample_rate)
            for path in chunk_paths:
                with wave.open(str(path), "rb") as source:
                    if (
                        source.getframerate() != sample_rate
                        or source.getnchannels() != channels
                        or source.getsampwidth() != width
                    ):
                        raise ValueError(f"incompatible WAV checkpoint: {path.name}")
                    while frames := source.readframes(65536):
                        output.writeframes(frames)
        with open(tmp, "rb") as written:
            os.fsync(written.fileno())
        os.replace(tmp, final_wav)
        return sample_rate
    finally:
        tmp.unlink(missing_ok=True)


def encode_mp3(final_wav: Path, final_mp3: Path, bitrate: str = "64k") -> str | None:
    """Encode an MP3 atomically. Returns a warning instead of failing a valid WAV job."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return "ffmpeg is unavailable; final WAV was generated but MP3 was skipped."
    final_mp3.parent.mkdir(parents=True, exist_ok=True)
    tmp = final_mp3.with_name(f".{final_mp3.stem}.part-{os.getpid()}.mp3")
    try:
        completed = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(final_wav),
                "-c:a",
                "libmp3lame",
                "-b:a",
                bitrate,
                str(tmp),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            return f"ffmpeg could not encode MP3: {completed.stderr.strip() or 'unknown error'}"
        os.replace(tmp, final_mp3)
        return None
    finally:
        tmp.unlink(missing_ok=True)

