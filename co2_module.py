"""
CO2 Module — reads gas values from the BME680 sensor via MQTT.

Subscribes to:
  /sensor/bme680/gas   → raw gas/CO2 ppm value published by the BME680

Publishes:
  sensor/CO2/alert     — "true" / "false" when threshold is exceeded

Falls back to Ornstein-Uhlenbeck demo values if the MQTT broker is
unreachable, but still attempts to connect and publish the alert.

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
from utils import is_demo_mode, setup_logging

logger = setup_logging("co2")

RECONNECT_DELAY = 5.0

# Ornstein-Uhlenbeck parameters for realistic CO2 simulation
_OU_THETA = 0.05   # mean-reversion speed
_OU_MU = 800.0     # long-term mean (ppm)
_OU_SIGMA = 50.0   # volatility

SOURCE_TOPIC = "sensor/bme680/gas"


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
# Main loop
# ---------------------------------------------------------------------------

async def _run_loop(cfg: AppConfig, demo: bool) -> None:
    """
    Subscribe to the BME680 gas topic and publish CO2/alert.

    In demo mode the loop generates synthetic ppm values locally and
    publishes the derived alert on every reconnect attempt.
    """
    current_ppm = _OU_MU
    interval = cfg.co2.read_interval_seconds

    while True:
        try:
            async with _build_client(cfg) as client:
                logger.info(f"Connected to MQTT broker {cfg.mqtt.broker}:{cfg.mqtt.port}")

                if demo:
                    # No real sensor — publish simulated alerts at fixed interval
                    while True:
                        current_ppm = _next_demo_co2(current_ppm, float(interval))
                        ppm = int(current_ppm)
                        alert = ppm > cfg.co2.threshold_ppm
                        await client.publish("sensor/CO2/alert", str(alert).lower(), retain=True)
                        logger.debug(f"[DEMO] CO2: {ppm} ppm | alert={alert}")
                        await asyncio.sleep(interval)
                else:
                    # Subscribe to BME680 gas topic; publish alert on each reading
                    await client.subscribe(SOURCE_TOPIC)
                    logger.info(f"Subscribed to {SOURCE_TOPIC}")
                    async for msg in client.messages:
                        try:
                            ppm = int(float(msg.payload.decode(errors="replace").strip()))
                        except ValueError as e:
                            logger.warning(f"Unexpected gas payload: {msg.payload!r} — {e}")
                            continue
                        alert = ppm > cfg.co2.threshold_ppm
                        await client.publish("sensor/CO2/alert", str(alert).lower(), retain=True)
                        logger.info(f"CO2: {ppm} ppm | alert={alert}")

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
    demo = is_demo_mode(
        lambda: socket.create_connection((cfg.mqtt.broker, cfg.mqtt.port), timeout=2)
    )
    if demo:
        logger.warning(
            f"DEMO MODE: broker {cfg.mqtt.broker}:{cfg.mqtt.port} unreachable — "
            "simulating CO2 drift"
        )

    await _run_loop(cfg, demo)


if __name__ == "__main__":
    asyncio.run(main())
