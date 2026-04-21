"""
Display Module — ILI9341 2.8" TFT LCD via 8-bit parallel interface.

Renders a top-down radar view with zone rectangles, live target dots,
and a status bar. Subscribes to MQTT for target positions, zone
occupancy, presence count, and CO2 alert.

Pin mapping (BCM numbers, tied to physical pins per pinout table):
  RST=25(22), CS=8(24), RS=24(18), WR=23(16), RD→3.3V
  D0=GPIO0(27), D1=GPIO1(28), D2=GPIO2(3),  D3=GPIO3(5)
  D4=GPIO4(7), D5=GPIO17(11), D6=GPIO27(13), D7=GPIO22(15)

Auto-switches to DEMO MODE (stdout) if RPi.GPIO is unavailable.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import aiomqtt

from config import AppConfig, load_config
from utils import get_ip_address, get_uptime, setup_logging

logger = setup_logging("display")

RENDER_INTERVAL = 1.0
RECONNECT_DELAY = 5.0

# Radar field-of-view in mm (sensor sits at bottom centre, Y=0)
RADAR_X_MIN, RADAR_X_MAX = -3500, 3500
RADAR_Y_MIN, RADAR_Y_MAX = 0, 5000

# Layout constants for 240×320 portrait
HEADER_H = 30
RADAR_H = 240
RADAR_W = 240
STATUS_H = 50          # = 320 - HEADER_H - RADAR_H

# Colours (R, G, B)
C_BG_RADAR   = (8,  10, 28)
C_GRID       = (35, 40, 80)
C_ZONE_EMPTY = (0, 80, 180)
C_ZONE_OCC   = (0, 200, 60)
C_ZONE_FILL  = (0, 60, 10)
C_TARGET     = (240, 50, 50)
C_SENSOR     = (180, 180, 255)
C_HDR_PRESENT = (0, 110, 0)
C_HDR_ABSENT  = (50, 50, 50)
C_STATUS_BG   = (18, 18, 18)
C_WHITE       = (255, 255, 255)
C_GREY        = (160, 160, 160)
C_ORANGE      = (255, 120, 0)
C_GREEN       = (80, 200, 80)


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

@dataclass
class DisplayState:
    count: int = 0
    presence: bool = False
    co2_alert: bool = False
    targets: list = field(default_factory=lambda: [{}, {}, {}])
    zone_occupied: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ILI9341 8-bit parallel driver
# ---------------------------------------------------------------------------

class ILI9341Parallel:
    """Minimal ILI9341 driver over 8-bit parallel GPIO (write-only)."""

    _CMD_SWRESET = 0x01
    _CMD_SLPOUT  = 0x11
    _CMD_COLMOD  = 0x3A
    _CMD_MADCTL  = 0x36
    _CMD_CASET   = 0x2A
    _CMD_RASET   = 0x2B
    _CMD_RAMWR   = 0x2C
    _CMD_DISPON  = 0x29

    def __init__(self, cfg):
        import RPi.GPIO as GPIO
        self._G = GPIO
        self._rst  = cfg.rst_pin
        self._cs   = cfg.cs_pin
        self._rs   = cfg.rs_pin
        self._wr   = cfg.wr_pin
        self._data = cfg.data_pins   # [D0..D7] as BCM pin numbers

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in [self._rst, self._cs, self._rs, self._wr] + self._data:
            GPIO.setup(pin, GPIO.OUT)

        self._reset()
        self._init()

    # -- low-level --

    def _reset(self):
        G = self._G
        G.output(self._rst, G.HIGH); time.sleep(0.01)
        G.output(self._rst, G.LOW);  time.sleep(0.02)
        G.output(self._rst, G.HIGH); time.sleep(0.15)

    def _write_byte(self, byte: int) -> None:
        G = self._G
        # Set D0–D7 then pulse WR
        G.output(self._data, [(byte >> i) & 1 for i in range(8)])
        G.output(self._wr, G.LOW)
        G.output(self._wr, G.HIGH)

    def _cmd(self, cmd: int) -> None:
        G = self._G
        G.output(self._cs, G.LOW)
        G.output(self._rs, G.LOW)
        self._write_byte(cmd)
        G.output(self._cs, G.HIGH)

    def _data_bytes(self, *args: int) -> None:
        G = self._G
        G.output(self._cs, G.LOW)
        G.output(self._rs, G.HIGH)
        for b in args:
            self._write_byte(b)
        G.output(self._cs, G.HIGH)

    # -- init sequence --

    def _init(self) -> None:
        # Full Adafruit/standard ILI9341 power-on sequence.
        # Minimal init (only COLMOD+MADCTL+DISPON) leaves most panels blank.
        self._cmd(0xEF); self._data_bytes(0x03, 0x80, 0x02)
        self._cmd(0xCF); self._data_bytes(0x00, 0xC1, 0x30)
        self._cmd(0xED); self._data_bytes(0x64, 0x03, 0x12, 0x81)
        self._cmd(0xE8); self._data_bytes(0x85, 0x00, 0x78)
        self._cmd(0xCB); self._data_bytes(0x39, 0x2C, 0x00, 0x34, 0x02)
        self._cmd(0xF7); self._data_bytes(0x20)
        self._cmd(0xEA); self._data_bytes(0x00, 0x00)
        # Power control
        self._cmd(0xC0); self._data_bytes(0x23)
        self._cmd(0xC1); self._data_bytes(0x10)
        # VCOM
        self._cmd(0xC5); self._data_bytes(0x3E, 0x28)
        self._cmd(0xC7); self._data_bytes(0x86)
        # Memory access & pixel format
        self._cmd(self._CMD_MADCTL); self._data_bytes(0x48)   # portrait, BGR
        self._cmd(self._CMD_COLMOD); self._data_bytes(0x55)   # RGB565
        # Frame rate (79 Hz)
        self._cmd(0xB1); self._data_bytes(0x00, 0x18)
        # Display function control
        self._cmd(0xB6); self._data_bytes(0x08, 0x82, 0x27)
        # 3-Gamma off, gamma curve 1
        self._cmd(0xF2); self._data_bytes(0x00)
        self._cmd(0x26); self._data_bytes(0x01)
        # Positive gamma correction
        self._cmd(0xE0); self._data_bytes(
            0x0F, 0x31, 0x2B, 0x0C, 0x0E, 0x08,
            0x4E, 0xF1, 0x37, 0x07, 0x10, 0x03,
            0x0E, 0x09, 0x00)
        # Negative gamma correction
        self._cmd(0xE1); self._data_bytes(
            0x00, 0x0E, 0x14, 0x03, 0x11, 0x07,
            0x31, 0xC1, 0x48, 0x08, 0x0F, 0x0C,
            0x31, 0x36, 0x0F)
        # Exit sleep, then display on
        self._cmd(self._CMD_SLPOUT); time.sleep(0.12)
        self._cmd(self._CMD_DISPON)

    # -- public API --

    def display(self, image) -> None:
        """Blit a 240×320 PIL RGB image to the display."""
        w, h = image.size
        # Set address window
        self._cmd(self._CMD_CASET)
        self._data_bytes(0, 0, 0, w - 1)
        self._cmd(self._CMD_RASET)
        self._data_bytes(0, 0, (h - 1) >> 8, (h - 1) & 0xFF)
        self._cmd(self._CMD_RAMWR)

        img_rgb = image.convert("RGB")
        raw = img_rgb.tobytes()   # R,G,B,R,G,B,… — avoids deprecated getdata()
        G = self._G
        G.output(self._cs, G.LOW)
        G.output(self._rs, G.HIGH)
        for i in range(0, len(raw), 3):
            r, g, b = raw[i], raw[i + 1], raw[i + 2]
            self._write_byte((r & 0xF8) | (g >> 5))
            self._write_byte(((g & 0x1C) << 3) | (b >> 3))
        G.output(self._cs, G.HIGH)

    def cleanup(self) -> None:
        self._G.cleanup()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _load_font(size: int):
    from PIL import ImageFont
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            pass
    # Pillow ≥10.1 accepts size= on the built-in bitmap font
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _mm_to_px(x_mm: float, y_mm: float) -> tuple[int, int]:
    """Convert sensor mm coords to pixel coords inside the radar area."""
    px = int((x_mm - RADAR_X_MIN) / (RADAR_X_MAX - RADAR_X_MIN) * RADAR_W)
    # Y=0 (sensor) → bottom of radar area; Y_MAX → top
    py = HEADER_H + RADAR_H - 1 - int(
        (y_mm - RADAR_Y_MIN) / (RADAR_Y_MAX - RADAR_Y_MIN) * RADAR_H
    )
    return (
        max(0, min(RADAR_W - 1, px)),
        max(HEADER_H, min(HEADER_H + RADAR_H - 1, py)),
    )


def _render_frame(state: DisplayState, cfg: AppConfig):
    from PIL import Image, ImageDraw

    img  = Image.new("RGB", (240, 320), C_STATUS_BG)
    draw = ImageDraw.Draw(img)
    font_sm = _load_font(12)
    font_md = _load_font(14)

    # ── Header ──────────────────────────────────────────────────────────
    hdr_color = C_HDR_PRESENT if state.presence else C_HDR_ABSENT
    draw.rectangle([(0, 0), (239, HEADER_H - 1)], fill=hdr_color)
    draw.text((6, 7), f"Personen: {state.count}", font=font_md, fill=C_WHITE)
    if state.co2_alert:
        draw.text((160, 7), "CO2!", font=font_md, fill=C_ORANGE)

    # ── Radar background ─────────────────────────────────────────────────
    draw.rectangle(
        [(0, HEADER_H), (RADAR_W - 1, HEADER_H + RADAR_H - 1)],
        fill=C_BG_RADAR,
    )

    # Range rings at 1000 / 2000 / 3000 / 4000 mm
    for r_mm in (1000, 2000, 3000, 4000):
        _, py = _mm_to_px(0, r_mm)
        draw.line([(0, py), (RADAR_W - 1, py)], fill=C_GRID)
        draw.text((2, py - 11), f"{r_mm//1000}m", font=_load_font(9), fill=C_GRID)

    # Centre line (x=0)
    cx, _ = _mm_to_px(0, 0)
    draw.line([(cx, HEADER_H), (cx, HEADER_H + RADAR_H - 1)], fill=C_GRID)

    # ── Zones ────────────────────────────────────────────────────────────
    for zone in cfg.zones:
        x0, y0 = _mm_to_px(zone.x_min, zone.y_max)
        x1, y1 = _mm_to_px(zone.x_max, zone.y_min)
        occupied = state.zone_occupied.get(zone.name, False)
        if occupied:
            draw.rectangle([(x0, y0), (x1, y1)], fill=C_ZONE_FILL, outline=C_ZONE_OCC, width=2)
        else:
            draw.rectangle([(x0, y0), (x1, y1)], outline=C_ZONE_EMPTY, width=1)
        # Label inside zone
        lx = (x0 + x1) // 2 - len(zone.name) * 3
        ly = (y0 + y1) // 2 - 6
        draw.text((lx, ly), zone.name[:6], font=_load_font(10), fill=C_WHITE)

    # ── Targets ──────────────────────────────────────────────────────────
    for t in state.targets:
        if t.get("x") is not None and t.get("y") is not None:
            tx, ty = _mm_to_px(t["x"], t["y"])
            draw.ellipse([(tx - 5, ty - 5), (tx + 5, ty + 5)], fill=C_TARGET)

    # Sensor indicator (triangle at bottom-centre of radar area)
    sx, _ = _mm_to_px(0, 0)
    sy = HEADER_H + RADAR_H - 1
    draw.polygon(
        [(sx, sy - 10), (sx - 7, sy), (sx + 7, sy)],
        fill=C_SENSOR,
    )

    # ── Status bar ───────────────────────────────────────────────────────
    sy0 = HEADER_H + RADAR_H
    ip     = get_ip_address()
    uptime = get_uptime()
    alert_text  = "CO2: ALARM!" if state.co2_alert else "CO2: OK"
    alert_color = C_ORANGE if state.co2_alert else C_GREEN

    draw.text((6, sy0 + 4),  f"IP: {ip}",      font=font_sm, fill=C_GREY)
    draw.text((6, sy0 + 20), f"Up: {uptime}",  font=font_sm, fill=C_GREY)
    draw.text((6, sy0 + 36), alert_text,        font=font_sm, fill=alert_color)

    return img


def _demo_render(state: DisplayState, cfg: AppConfig) -> None:
    zones_str = ", ".join(
        f"{z.name}={'ON' if state.zone_occupied.get(z.name) else 'off'}"
        for z in cfg.zones
    )
    targets_active = [t for t in state.targets if t.get("x") is not None]
    logger.info(
        f"[RENDER] Personen:{state.count} {'ANWESEND' if state.presence else 'leer'} | "
        f"Zonen:[{zones_str}] | Targets:{len(targets_active)} | "
        f"CO2:{'ALARM' if state.co2_alert else 'ok'} | "
        f"IP:{get_ip_address()} Up:{get_uptime()}"
    )


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


async def _mqtt_listener(cfg: AppConfig, state: DisplayState) -> None:
    topics = [
        "sensor/HLK-LD2450/count",
        "sensor/HLK-LD2450/presence",
        "sensor/HLK-LD2450/target_0",
        "sensor/HLK-LD2450/target_1",
        "sensor/HLK-LD2450/target_2",
        "sensor/HLK-LD2450/zone/+",
        "sensor/CO2/alert",
    ]
    while True:
        try:
            async with _build_client(cfg) as client:
                for t in topics:
                    await client.subscribe(t)
                logger.info("MQTT listener subscribed")
                async for msg in client.messages:
                    topic   = str(msg.topic)
                    payload = msg.payload.decode(errors="replace").strip()
                    if topic == "sensor/HLK-LD2450/count":
                        state.count = int(payload)
                    elif topic == "sensor/HLK-LD2450/presence":
                        state.presence = payload == "true"
                    elif topic == "sensor/CO2/alert":
                        state.co2_alert = payload == "true"
                    elif topic.startswith("sensor/HLK-LD2450/target_"):
                        idx = int(topic[-1])
                        try:
                            data = json.loads(payload)
                            state.targets[idx] = data if data else {}
                        except (json.JSONDecodeError, ValueError):
                            state.targets[idx] = {}
                    elif topic.startswith("sensor/HLK-LD2450/zone/"):
                        zone_name = topic.split("/")[-1]
                        state.zone_occupied[zone_name] = payload == "true"
        except aiomqtt.MqttError as e:
            logger.warning(f"MQTT error: {e} — reconnecting in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)
        except Exception as e:
            logger.error(f"Listener error: {e} — reconnecting in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)


# ---------------------------------------------------------------------------
# Render loop
# ---------------------------------------------------------------------------

async def _render_loop(cfg: AppConfig, state: DisplayState, device) -> None:
    while True:
        try:
            if device is not None:
                image = _render_frame(state, cfg)
                # GPIO writes are blocking → run in thread so event loop stays alive
                await asyncio.to_thread(device.display, image)
            else:
                _demo_render(state, cfg)
        except Exception as e:
            logger.error(f"Render error: {e}")
        await asyncio.sleep(RENDER_INTERVAL)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    cfg   = load_config()
    state = DisplayState()

    device = None
    try:
        device = ILI9341Parallel(cfg.display)
        logger.info("ILI9341 TFT initialised (240×320, 8-bit parallel)")
    except Exception as e:
        logger.warning(f"No TFT hardware ({e}) — rendering to stdout")

    await asyncio.gather(
        _mqtt_listener(cfg, state),
        _render_loop(cfg, state, device),
    )


if __name__ == "__main__":
    asyncio.run(main())
