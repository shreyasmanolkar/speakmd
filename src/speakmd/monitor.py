"""Inexpensive, best-effort process and NVIDIA GPU monitoring."""

from __future__ import annotations

import shutil
import subprocess
import time

import psutil


class ResourceMonitor:
    def __init__(self) -> None:
        self._gpu_cache: dict = {"available": False, "reason": "nvidia-smi not found"}
        self._gpu_at = 0.0
        psutil.cpu_percent(interval=None)

    def snapshot(self) -> dict:
        memory = psutil.virtual_memory()
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "ram": {"used": memory.used, "total": memory.total, "percent": memory.percent},
            "gpu": self._gpu(),
        }

    def _gpu(self) -> dict:
        if time.monotonic() - self._gpu_at < 1:
            return self._gpu_cache
        self._gpu_at = time.monotonic()
        binary = shutil.which("nvidia-smi")
        if not binary:
            self._gpu_cache = {"available": False, "reason": "nvidia-smi not found"}
            return self._gpu_cache
        try:
            command = [
                binary,
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=0.7, check=False)
            if result.returncode:
                self._gpu_cache = {"available": False, "reason": result.stderr.strip() or "nvidia-smi failed"}
                return self._gpu_cache
            devices = []
            for row in result.stdout.splitlines():
                name, util, used, total = [part.strip() for part in row.split(",", 3)]
                devices.append({"name": name, "utilization_percent": float(util), "memory_used_mib": float(used), "memory_total_mib": float(total)})
            self._gpu_cache = {"available": bool(devices), "devices": devices}
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            self._gpu_cache = {"available": False, "reason": str(exc)}
        return self._gpu_cache

