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
import time
from typing import AsyncIterator

import serial

from config import SensorConfig, load_config
from utils import STREAM_PATH, is_demo_mode, run_in_executor, setup_logging, write_stream_file_atomic

logger = setup_logging("sensor")

# HLK-LD2450 binary frame constants
FRAME_HEADER = bytes([0xAA, 0xFF, 0x03, 0x00])
FRAME_FOOTER = bytes([0x55, 0xCC])
FRAME_SIZE   = 30   # 4 header + 3×8 target slots + 2 footer
MAX_TARGETS  = 3

# Demo parameters per target: (amp_x, amp_y, omega, phase_x, phase_y, offset_y)
_DEMO_PARAMS = [
    (400, 500, 0.4, 0.0, 0.0,  800),
    (300, 600, 0.3, 1.0, 2.0, 1600),
    (250, 400, 0.5, 2.5, 1.0, 1200),
]


# ---------------------------------------------------------------------------
# Frame parsing
# ---------------------------------------------------------------------------

def _decode_coord(raw: int) -> int:
    """
    HLK-LD2450 non-standard sign encoding:
      - Read bytes as standard signed int16 (little-endian).
      - If the result is negative (bit 15 set), the true value is: -32768 - raw
    This converts the sensor's direction-bit encoding into a real signed integer.
    Reference: https://github.com/csRon/HLK-LD2450  serial_protocol.py
    """
    return raw if raw >= 0 else -32768 - raw


def _parse_frame(frame: bytes) -> list[dict]:
    """
    Parse a 30-byte HLK-LD2450 frame.

    Frame layout:
        [0:4]   header  AA FF 03 00
        [4:12]  target 1: x(int16LE), y(int16LE), speed(int16LE), res(uint16LE)
        [12:20] target 2
        [20:28] target 3
        [28:30] footer  55 CC

    Coordinate convention after decoding:
        x: lateral mm  (negative = left,  positive = right)
        y: depth mm    (sensor outputs negative = in front; we negate → positive distance)

    An empty slot is indicated by all 8 bytes being 0x00.
    """
    if len(frame) < FRAME_SIZE:
        return []

    # Locate header inside the received bytes (read_until may prepend garbage)
    idx = frame.find(FRAME_HEADER)
    if idx == -1 or idx + FRAME_SIZE > len(frame):
        return []
    frame = frame[idx:idx + FRAME_SIZE]
    if frame[28:30] != FRAME_FOOTER:
        return []

    targets = []
    for i in range(MAX_TARGETS):
        off  = 4 + i * 8
        slot = frame[off:off + 8]
        if slot == bytes(8):          # empty slot → all zero
            continue

        x_raw = int.from_bytes(slot[0:2], 'little', signed=True)
        y_raw = int.from_bytes(slot[2:4], 'little', signed=True)

        x =  _decode_coord(x_raw)
        y = -_decode_coord(y_raw)    # negate: sensor y<0 in front → positive distance

        targets.append({"id": i + 1, "x": x, "y": y})
    return targets


# ---------------------------------------------------------------------------
# Real hardware loop
# ---------------------------------------------------------------------------

async def _open_serial(cfg: SensorConfig) -> serial.Serial:
    def _open():
        return serial.Serial(cfg.port, cfg.baudrate, timeout=cfg.timeout)
    return await run_in_executor(_open)


async def _frame_stream(ser: serial.Serial) -> AsyncIterator[bytes]:
    """
    Use read_until(FRAME_FOOTER) — the same approach as the reference implementation.
    This is simpler and more reliable than chunk-scanning: pyserial blocks until the
    footer bytes arrive, then returns exactly one frame worth of data.
    """
    def _read_frame():
        return ser.read_until(FRAME_FOOTER)
    while True:
        frame = await run_in_executor(_read_frame)
        if frame:
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
