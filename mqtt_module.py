"""
MQTT Module — bridge between the sensor file stream and the MQTT broker.

Publishes at 2 Hz:
  sensor/HLK-LD2450/target_<n>   — individual target JSON
  sensor/HLK-LD2450/targets       — all targets as JSON array
  sensor/HLK-LD2450/presence      — "true" / "false"
  sensor/HLK-LD2450/count         — integer string
  sensor/HLK-LD2450/zone/<name>   — "true" / "false" per configured zone

Subscribes to:
  sensor/HLK-LD2450/command       — runtime config commands (reserved)

Auto-switches to DEMO MODE if the broker is unreachable or the stream
file is absent.

Usage:
    python mqtt_module.py
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import socket
import time
from typing import Optional

import aiomqtt

from config import AppConfig, load_config
from utils import (
    STREAM_PATH,
    is_demo_mode,
    read_stream_file,
    setup_logging,
    targets_in_zone,
)

logger = setup_logging("mqtt")

PUBLISH_INTERVAL = 0.5  # 2 Hz
RECONNECT_DELAY = 5.0

# Demo target motion params (same as sensor_module demo for visual consistency)
_DEMO_PARAMS = [
    (400, 500, 0.4, 0.0, 0.0, 800),
    (300, 600, 0.3, 1.0, 2.0, 1600),
]

_demo_t: float = 0.0
_last_known: dict = {"targets": [], "timestamp": 0.0}


# ---------------------------------------------------------------------------
# Demo data generation
# ---------------------------------------------------------------------------

def _generate_demo_data() -> dict:
    global _demo_t
    targets = []
    for i, (ax, ay, omega, px, py, oy) in enumerate(_DEMO_PARAMS):
        x = int(ax * math.sin(omega * _demo_t + px))
        y = int(ay * math.sin(omega * _demo_t + py) + oy)
        targets.append({"id": i + 1, "x": x, "y": y})
    _demo_t += PUBLISH_INTERVAL
    return {"targets": targets, "timestamp": time.time()}


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


async def _publish_data(client: aiomqtt.Client, data: dict, cfg: AppConfig) -> None:
    targets: list[dict] = data.get("targets", [])
    presence = len(targets) > 0

    # Individual targets
    published_ids = set()
    for t in targets:
        tid = t["id"]
        published_ids.add(tid)
        await client.publish(f"sensor/HLK-LD2450/target_{tid}", json.dumps(t), retain=True)

    # Clear slots that have no target this cycle
    for i in range(1, 4):
        if i not in published_ids:
            await client.publish(f"sensor/HLK-LD2450/target_{i}", json.dumps({}), retain=True)

    # Aggregate topics
    await client.publish("sensor/HLK-LD2450/targets", json.dumps(targets), retain=True)
    await client.publish("sensor/HLK-LD2450/presence", str(presence).lower(), retain=True)
    await client.publish("sensor/HLK-LD2450/count", str(len(targets)), retain=True)

    # Zone topics
    for zone in cfg.zones:
        occupied = targets_in_zone(targets, zone)
        await client.publish(
            f"sensor/HLK-LD2450/zone/{zone.name}",
            str(occupied).lower(),
            retain=True,
        )


# ---------------------------------------------------------------------------
# Command listener (runs as a concurrent task inside the MQTT connection)
# ---------------------------------------------------------------------------

async def _command_listener(client: aiomqtt.Client) -> None:
    await client.subscribe("sensor/HLK-LD2450/command")
    async for msg in client.messages:
        topic = str(msg.topic)
        if topic == "sensor/HLK-LD2450/command":
            payload = msg.payload.decode(errors="replace")
            logger.info(f"Received command: {payload}")
            # Reserved for future runtime config commands


# ---------------------------------------------------------------------------
# Main publish loop
# ---------------------------------------------------------------------------

async def _publish_loop(cfg: AppConfig, demo: bool) -> None:
    global _last_known
    while True:
        try:
            async with _build_client(cfg) as client:
                logger.info(f"Connected to MQTT broker {cfg.mqtt.broker}:{cfg.mqtt.port}")
                listener_task = asyncio.create_task(_command_listener(client))
                try:
                    while True:
                        if demo:
                            data = _generate_demo_data()
                        else:
                            data = read_stream_file(STREAM_PATH)
                            if data is None:
                                data = _last_known
                            else:
                                _last_known = data

                        await _publish_data(client, data, cfg)
                        await asyncio.sleep(PUBLISH_INTERVAL)
                finally:
                    listener_task.cancel()
                    try:
                        await listener_task
                    except asyncio.CancelledError:
                        pass

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
            "generating synthetic data (will still attempt MQTT publish)"
        )

    await _publish_loop(cfg, demo)


if __name__ == "__main__":
    asyncio.run(main())
