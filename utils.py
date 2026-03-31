"""
Shared utilities: file I/O, zone detection, networking, logging.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Callable, Optional

from config import ZoneConfig

STREAM_PATH = "/tmp/sensor_stream.json"
STREAM_STALE_SECONDS = 5.0


def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(name)


def is_demo_mode(resource_check_fn: Callable[[], Any]) -> bool:
    """Return True if calling resource_check_fn raises any Exception (hardware absent)."""
    try:
        result = resource_check_fn()
        # Close file-like objects returned by the check
        if hasattr(result, "close"):
            result.close()
        return False
    except Exception:
        return True


def write_stream_file_atomic(data: dict, path: str = STREAM_PATH) -> None:
    """Write JSON atomically via a .tmp sibling + os.replace."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def read_stream_file(path: str = STREAM_PATH) -> Optional[dict]:
    """
    Read and parse the sensor stream file.
    Returns None if the file is missing, unparseable, or stale (> STREAM_STALE_SECONDS).
    """
    try:
        raw = Path(path).read_text()
        data = json.loads(raw)
        ts = data.get("timestamp", 0)
        if time.time() - ts > STREAM_STALE_SECONDS:
            return None
        return data
    except Exception:
        return None


def targets_in_zone(targets: list[dict], zone: ZoneConfig) -> bool:
    """Return True if any target's (x, y) falls within the zone rectangle."""
    for t in targets:
        x, y = t.get("x", 0), t.get("y", 0)
        if zone.x_min <= x <= zone.x_max and zone.y_min <= y <= zone.y_max:
            return True
    return False


def get_ip_address() -> str:
    """Return the primary non-loopback IPv4 address, or '?.?.?.?' on failure."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "?.?.?.?"


def get_uptime() -> str:
    """Read /proc/uptime and return a formatted string like '3h 42m'."""
    try:
        seconds = float(Path("/proc/uptime").read_text().split()[0])
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m:02d}m"
    except Exception:
        return "N/A"


async def run_in_executor(fn: Callable, *args: Any) -> Any:
    """Run a blocking callable in the default thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn, *args)
