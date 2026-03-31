"""
CO2 Module — MH-Z19 sensor via UART or GPIO.

Reads CO2 ppm every N seconds and publishes:
  sensor/CO2/ppm    — integer ppm value
  sensor/CO2/alert  — "true" / "false" when threshold exceeded

Auto-switches to DEMO MODE (Ornstein-Uhlenbeck drift) if the sensor
port is unavailable or mh_z19 is not installed.

Usage:
    python co2_module.py
"""
from __future__ import annotations

import asyncio
import math
import random
import socket

import aiomqtt

from config import AppConfig, load_config
from utils import is_demo_mode, run_in_executor, setup_logging

logger = setup_logging("co2")

RECONNECT_DELAY = 5.0

# Ornstein-Uhlenbeck parameters for realistic CO2 simulation
_OU_THETA = 0.05   # mean-reversion speed
_OU_MU = 800.0     # long-term mean (ppm)
_OU_SIGMA = 50.0   # volatility


# ---------------------------------------------------------------------------
# Hardware reading
# ---------------------------------------------------------------------------

def _try_import_mhz19():
    import mh_z19
    return mh_z19


def _read_co2_blocking(port: str) -> int:
    mh_z19 = _try_import_mhz19()
    result = mh_z19.read_serial(port)
    if result is None:
        raise RuntimeError("mh_z19.read_serial returned None")
    ppm = result.get("co2")
    if ppm is None:
        raise RuntimeError(f"Unexpected mh_z19 response: {result}")
    return int(ppm)


async def _read_co2_async(port: str) -> int:
    return await run_in_executor(_read_co2_blocking, port)


# ---------------------------------------------------------------------------
# Demo simulation (Ornstein-Uhlenbeck process)
# ---------------------------------------------------------------------------

def _next_demo_co2(current: float, dt: float) -> float:
    noise = random.gauss(0, 1)
    next_val = (
        current
        + _OU_THETA * (_OU_MU - current) * dt
        + _OU_SIGMA * math.sqrt(dt) * noise
    )
    return max(400.0, min(2000.0, next_val))


# ---------------------------------------------------------------------------
# MQTT helpers
# ---------------------------------------------------------------------------

def _build_client(cfg: AppConfig) -> aiomqtt.Client:
    kwargs: dict = {
        "hostname": cfg.mqtt.broker,
        "port": cfg.mqtt.port,
        "keepalive": cfg.mqtt.keepalive,
    }
    if cfg.mqtt.username:
        kwargs["username"] = cfg.mqtt.username
    if cfg.mqtt.password:
        kwargs["password"] = cfg.mqtt.password
    return aiomqtt.Client(**kwargs)


# ---------------------------------------------------------------------------
# Main read loop
# ---------------------------------------------------------------------------

async def _read_loop(cfg: AppConfig, demo: bool) -> None:
    current_ppm = _OU_MU
    interval = cfg.co2.read_interval_seconds

    while True:
        try:
            async with _build_client(cfg) as client:
                logger.info(f"Connected to MQTT broker {cfg.mqtt.broker}:{cfg.mqtt.port}")
                while True:
                    if demo:
                        current_ppm = _next_demo_co2(current_ppm, float(interval))
                        ppm = int(current_ppm)
                        logger.debug(f"[DEMO] CO2: {ppm} ppm")
                    else:
                        try:
                            ppm = await _read_co2_async(cfg.co2.port)
                            current_ppm = float(ppm)
                        except Exception as e:
                            logger.error(f"CO2 read error: {e} — using last value {int(current_ppm)}")
                            ppm = int(current_ppm)

                    alert = ppm > cfg.co2.threshold_ppm
                    await client.publish("sensor/CO2/ppm", str(ppm), retain=True)
                    await client.publish("sensor/CO2/alert", str(alert).lower(), retain=True)
                    logger.info(f"CO2: {ppm} ppm | alert={alert}")
                    await asyncio.sleep(interval)

        except aiomqtt.MqttError as e:
            logger.warning(f"MQTT error: {e} — reconnecting in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)
        except Exception as e:
            logger.error(f"Unexpected error: {e} — reconnecting in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    cfg = load_config()

    def _check_co2():
        _read_co2_blocking(cfg.co2.port)

    demo = is_demo_mode(_check_co2)
    if demo:
        logger.warning("DEMO MODE: CO2 sensor unavailable — simulating CO2 drift")

    await _read_loop(cfg, demo)


if __name__ == "__main__":
    asyncio.run(main())
