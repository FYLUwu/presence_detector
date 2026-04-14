"""
Display Module — SSD1306 OLED via I2C (128×64 px).

Subscribes to MQTT and renders a 4-line status screen every second:
  Line 1: Person count + presence indicator
  Line 2: CO2 ppm with alert flag
  Line 3: Device IP address
  Line 4: System uptime

Auto-switches to DEMO MODE (stdout render) if the I2C device or
luma.oled is unavailable.

Usage:
    python display_module.py
"""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field

import aiomqtt

from config import AppConfig, load_config
from utils import get_ip_address, get_uptime, is_demo_mode, setup_logging

logger = setup_logging("display")

RENDER_INTERVAL = 1.0
RECONNECT_DELAY = 5.0


# ---------------------------------------------------------------------------
# Shared state between MQTT listener and render loop
# ---------------------------------------------------------------------------

@dataclass
class DisplayState:
    count: int = 0
    presence: bool = False
    co2_ppm: int = 0
    co2_alert: bool = False


# ---------------------------------------------------------------------------
# Hardware helpers
# ---------------------------------------------------------------------------

def _try_get_device(cfg: AppConfig):
    """Attempt to open the SSD1306 OLED device via luma.oled."""
    from luma.core.interface.serial import i2c as luma_i2c
    from luma.oled.device import ssd1306
    address = int(cfg.display.i2c_address, 16)
    serial_iface = luma_i2c(port=1, address=address)
    return ssd1306(serial_iface, width=cfg.display.width, height=cfg.display.height)


def _render_image(state: DisplayState, cfg: AppConfig):
    """Render the display image and return a PIL Image."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("1", (cfg.display.width, cfg.display.height), 0)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    presence_icon = "[P]" if state.presence else "[ ]"
    co2_flag = " !ALERT!" if state.co2_alert else ""
    ip = get_ip_address()
    uptime = get_uptime()

    draw.text((0, 0),  f"Persons: {state.count} {presence_icon}", font=font, fill=255)
    draw.text((0, 16), f"CO2: {state.co2_ppm} ppm{co2_flag}", font=font, fill=255)
    draw.text((0, 32), f"IP: {ip}", font=font, fill=255)
    draw.text((0, 48), f"Up: {uptime}", font=font, fill=255)
    return img


# ---------------------------------------------------------------------------
# MQTT listener task
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


async def _mqtt_listener(cfg: AppConfig, state: DisplayState) -> None:
    topics = [
        "sensor/HLK-LD2450/count",
        "sensor/HLK-LD2450/presence",
        "sensor/bme680/gas",
        "sensor/CO2/alert",
    ]
    while True:
        try:
            async with _build_client(cfg) as client:
                for t in topics:
                    await client.subscribe(t)
                logger.info("MQTT listener subscribed")
                async for msg in client.messages:
                    topic = str(msg.topic)
                    payload = msg.payload.decode(errors="replace").strip()
                    if topic == "sensor/HLK-LD2450/count":
                        state.count = int(payload)
                    elif topic == "sensor/HLK-LD2450/presence":
                        state.presence = payload == "true"
                    elif topic == "sensor/bme680/gas":
                        state.co2_ppm = int(float(payload))
                    elif topic == "sensor/CO2/alert":
                        state.co2_alert = payload == "true"
        except aiomqtt.MqttError as e:
            logger.warning(f"MQTT error: {e} — reconnecting in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)
        except Exception as e:
            logger.error(f"Listener error: {e} — reconnecting in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)


# ---------------------------------------------------------------------------
# Render loop
# ---------------------------------------------------------------------------

async def _render_loop(cfg: AppConfig, state: DisplayState, device=None) -> None:
    while True:
        try:
            image = _render_image(state, cfg)
            if device is not None:
                device.display(image)
            else:
                ip = get_ip_address()
                uptime = get_uptime()
                alert_flag = " !ALERT!" if state.co2_alert else ""
                logger.info(
                    f"[RENDER] Persons:{state.count} {'P' if state.presence else '_'} | "
                    f"CO2:{state.co2_ppm}ppm{alert_flag} | IP:{ip} | Up:{uptime}"
                )
        except Exception as e:
            logger.error(f"Render error: {e}")

        await asyncio.sleep(RENDER_INTERVAL)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    cfg = load_config()
    state = DisplayState()

    no_oled = is_demo_mode(lambda: _try_get_device(cfg))
    if no_oled:
        logger.warning("No OLED hardware — rendering to stdout")
        device = None
    else:
        device = _try_get_device(cfg)

    await asyncio.gather(
        _mqtt_listener(cfg, state),
        _render_loop(cfg, state, device=device),
    )


if __name__ == "__main__":
    asyncio.run(main())
