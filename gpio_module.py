"""
GPIO Module — controls output pins based on MQTT presence/alert topics.

Subscribes to:
  sensor/HLK-LD2450/presence  → presence_pin HIGH/LOW
  sensor/CO2/alert             → alert_pin HIGH/LOW

Auto-switches to DEMO MODE (log-only) if RPi.GPIO is unavailable
(e.g. on a non-Pi development machine).

Usage:
    python gpio_module.py
"""
from __future__ import annotations

import asyncio

import aiomqtt

from config import AppConfig, load_config
from utils import is_demo_mode, setup_logging

logger = setup_logging("gpio")

RECONNECT_DELAY = 5.0


# ---------------------------------------------------------------------------
# GPIO setup
# ---------------------------------------------------------------------------

def _setup_gpio(cfg: AppConfig):
    """Import and initialise RPi.GPIO. Raises ImportError/RuntimeError if unavailable."""
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(cfg.gpio.presence_pin, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(cfg.gpio.alert_pin, GPIO.OUT, initial=GPIO.LOW)
    return GPIO


def _set_pin(gpio, pin: int, high: bool, demo: bool) -> None:
    if demo:
        logger.info(f"[DEMO] GPIO {pin} → {'HIGH' if high else 'LOW'}")
    else:
        gpio.output(pin, gpio.HIGH if high else gpio.LOW)


# ---------------------------------------------------------------------------
# MQTT listener
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


async def _listener_loop(cfg: AppConfig, gpio, demo: bool) -> None:
    while True:
        try:
            async with _build_client(cfg) as client:
                await client.subscribe("sensor/HLK-LD2450/presence")
                await client.subscribe("sensor/CO2/alert")
                logger.info("GPIO module subscribed to MQTT topics")
                async for msg in client.messages:
                    topic = str(msg.topic)
                    payload = msg.payload.decode(errors="replace").strip()
                    high = payload == "true"

                    if topic == "sensor/HLK-LD2450/presence":
                        _set_pin(gpio, cfg.gpio.presence_pin, high, demo)
                    elif topic == "sensor/CO2/alert":
                        _set_pin(gpio, cfg.gpio.alert_pin, high, demo)

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
    demo = is_demo_mode(lambda: _setup_gpio(cfg))

    if demo:
        logger.warning("DEMO MODE: RPi.GPIO unavailable — logging pin states only")
        gpio = None
    else:
        gpio = _setup_gpio(cfg)

    try:
        await _listener_loop(cfg, gpio, demo)
    finally:
        if not demo and gpio is not None:
            try:
                gpio.cleanup()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
