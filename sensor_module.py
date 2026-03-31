"""
Sensor Module — HLK-LD2450 via UART.

Reads binary frames from the radar sensor, parses up to 3 targets,
and writes results atomically to /tmp/sensor_stream.json at ~10 Hz.

Auto-switches to DEMO MODE (smooth sinusoidal motion) if the serial
port is unavailable.

Usage:
    python sensor_module.py
"""
from __future__ import annotations

import asyncio
import math
import struct
import time
from typing import AsyncIterator

import serial

from config import AppConfig, SensorConfig, load_config
from utils import STREAM_PATH, is_demo_mode, run_in_executor, setup_logging, write_stream_file_atomic

logger = setup_logging("sensor")

# HLK-LD2450 binary frame layout
FRAME_HEADER = bytes([0xAA, 0xFF, 0x03, 0x00])
FRAME_FOOTER = bytes([0x55, 0xCC])
FRAME_SIZE = 30  # 4 header + 3*8 targets + 2 footer
MAX_TARGETS = 3

# Demo parameters per target: (amplitude_x, amplitude_y, omega, phase_x, phase_y, offset_y)
_DEMO_PARAMS = [
    (400, 500, 0.4, 0.0, 0.0, 800),
    (300, 600, 0.3, 1.0, 2.0, 1600),
    (250, 400, 0.5, 2.5, 1.0, 1200),
]


# ---------------------------------------------------------------------------
# Frame parsing
# ---------------------------------------------------------------------------

def _parse_frame(frame: bytes) -> list[dict]:
    """
    Parse a 30-byte HLK-LD2450 frame into a list of target dicts.

    Frame layout:
        [0:4]  header  AA FF 03 00
        [4:12] target 1: x(int16LE), y(int16LE), speed(int16LE), res(uint16LE)
        [12:20] target 2
        [20:28] target 3
        [28:30] footer 55 CC
    """
    if len(frame) != FRAME_SIZE:
        return []
    if frame[:4] != FRAME_HEADER or frame[28:30] != FRAME_FOOTER:
        return []

    targets = []
    for i in range(MAX_TARGETS):
        offset = 4 + i * 8
        slot = frame[offset:offset + 8]
        x, y, speed, _ = struct.unpack_from("<hhhH", slot)
        if x == 0 and y == 0:
            continue
        targets.append({"id": i + 1, "x": int(x), "y": int(y)})
    return targets


def _find_frame(buf: bytearray) -> tuple[bytes | None, bytearray]:
    """
    Scan buf for a valid 30-byte frame.
    Returns (frame_bytes, remaining_buffer) or (None, buf) if not found.
    """
    idx = buf.find(FRAME_HEADER)
    if idx == -1:
        # Keep last 3 bytes in case the header straddles a read boundary
        return None, buf[-3:]
    if idx + FRAME_SIZE > len(buf):
        return None, buf[idx:]
    candidate = bytes(buf[idx:idx + FRAME_SIZE])
    if candidate[28:30] == FRAME_FOOTER:
        return candidate, buf[idx + FRAME_SIZE:]
    # Header found but footer mismatch; advance past this header and retry
    return None, buf[idx + 1:]


# ---------------------------------------------------------------------------
# Real hardware loop
# ---------------------------------------------------------------------------

async def _open_serial(cfg: SensorConfig) -> serial.Serial:
    def _open():
        return serial.Serial(cfg.port, cfg.baudrate, timeout=cfg.timeout)
    return await run_in_executor(_open)


async def _read_chunk(ser: serial.Serial, size: int = 64) -> bytes:
    def _read():
        return ser.read(size)
    return await run_in_executor(_read)


async def _frame_stream(ser: serial.Serial) -> AsyncIterator[bytes]:
    buf: bytearray = bytearray()
    while True:
        chunk = await _read_chunk(ser)
        if not chunk:
            await asyncio.sleep(0.01)
            continue
        buf.extend(chunk)
        while True:
            frame, buf = _find_frame(buf)
            if frame is None:
                break
            yield frame


async def _real_loop(cfg: SensorConfig) -> None:
    while True:
        try:
            logger.info(f"Opening serial port {cfg.port} @ {cfg.baudrate} baud")
            ser = await _open_serial(cfg)
            logger.info("Serial port opened successfully")
            async for frame in _frame_stream(ser):
                targets = _parse_frame(frame)
                write_stream_file_atomic(
                    {"targets": targets, "timestamp": time.time()},
                    STREAM_PATH,
                )
        except serial.SerialException as e:
            logger.warning(f"Serial error: {e} — reconnecting in 2s")
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Unexpected error in real loop: {e} — reconnecting in 5s")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Demo loop
# ---------------------------------------------------------------------------

async def _demo_loop() -> None:
    logger.info("DEMO MODE: simulating moving targets")
    t = 0.0
    dt = 0.1  # 10 Hz

    while True:
        targets = []
        for i, (ax, ay, omega, px, py, oy) in enumerate(_DEMO_PARAMS):
            x = int(ax * math.sin(omega * t + px))
            y = int(ay * math.sin(omega * t + py) + oy)
            targets.append({"id": i + 1, "x": x, "y": y})

        write_stream_file_atomic(
            {"targets": targets, "timestamp": time.time()},
            STREAM_PATH,
        )
        t += dt
        await asyncio.sleep(dt)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    cfg = load_config()
    demo = is_demo_mode(
        lambda: serial.Serial(cfg.sensor.port, cfg.sensor.baudrate, timeout=0.1)
    )

    if demo:
        await _demo_loop()
    else:
        await _real_loop(cfg.sensor)


if __name__ == "__main__":
    asyncio.run(main())
