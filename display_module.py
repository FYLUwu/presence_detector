"""
Display Module — ILI9341 2.8" TFT LCD via 8-bit parallel interface.

Renders a top-down radar view identical in spirit to the web UI: zones
(blue outline / green fill when occupied), target dots (red), range rings,
and a status bar.

There is no mature Python library for 8-bit parallel ILI9341 — fbtft's
parallel support is unmaintained, luma.lcd / Adafruit libraries are
SPI-only. So we talk to the GPIO hardware directly by memory-mapping
/dev/gpiomem, which is ~100× faster than RPi.GPIO (needed because a
full frame is ~153 kB = 1.2 M GPIO toggles).

Pin mapping (BCM, physical):
  RST=25(22), CS=8(24), RS=24(18), WR=23(16), RD→3.3V
  D0=GPIO0(27), D1=GPIO1(28), D2=GPIO2(3),  D3=GPIO3(5)
  D4=GPIO4(7), D5=GPIO17(11), D6=GPIO27(13), D7=GPIO22(15)

Falls back to stdout demo mode if /dev/gpiomem isn't available.
"""
from __future__ import annotations

import asyncio
import json
import mmap
import os
import struct
import time
from dataclasses import dataclass, field

import aiomqtt

from config import AppConfig, load_config
from utils import get_ip_address, get_uptime, setup_logging

logger = setup_logging("display")

RENDER_INTERVAL = 0.25         # 4 Hz target rate (actual rate bounded by GPIO speed)
RECONNECT_DELAY = 5.0

# Radar field-of-view in mm (sensor sits at bottom centre, Y=0)
RADAR_X_MIN, RADAR_X_MAX = -3500, 3500
RADAR_Y_MIN, RADAR_Y_MAX = 0, 5000

# Layout for 240×320 portrait
HEADER_H = 28
RADAR_H  = 242
RADAR_W  = 240
STATUS_H = 50     # 28 + 242 + 50 = 320

# Colours (R, G, B) — chosen for good contrast on TFT
C_BG_RADAR    = (8,  10, 28)
C_GRID        = (35, 40, 80)
C_ZONE_EMPTY  = (0, 110, 220)
C_ZONE_OCC    = (0, 220, 70)
C_ZONE_FILL   = (0, 70, 20)
C_TARGET      = (240, 50, 50)
C_SENSOR      = (180, 180, 255)
C_HDR_PRESENT = (0, 130, 0)
C_HDR_ABSENT  = (50, 50, 50)
C_STATUS_BG   = (18, 18, 18)
C_WHITE       = (255, 255, 255)
C_GREY        = (170, 170, 170)
C_ORANGE      = (255, 130, 0)
C_GREEN       = (80, 220, 80)


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
# Fast memory-mapped GPIO (bypasses RPi.GPIO for ~100× throughput)
# ---------------------------------------------------------------------------

class _FastGPIO:
    """Direct access to BCM GPIO via /dev/gpiomem.

    /dev/gpiomem maps the GPIO peripheral starting at offset 0, independent of
    the SoC base address. Works on Pi 1 through Pi 5.
    """
    _GPFSEL0 = 0x00    # Function select (pins 0-9); each pin = 3 bits
    _GPSET0  = 0x1C    # Write 1 bits = drive HIGH   (pins 0-31)
    _GPCLR0  = 0x28    # Write 1 bits = drive LOW    (pins 0-31)

    def __init__(self) -> None:
        fd = os.open("/dev/gpiomem", os.O_RDWR | os.O_SYNC)
        try:
            self._mem = mmap.mmap(
                fd, 4096,
                flags=mmap.MAP_SHARED,
                prot=mmap.PROT_READ | mmap.PROT_WRITE,
            )
        finally:
            os.close(fd)

    def setup_output(self, pin: int) -> None:
        reg = self._GPFSEL0 + (pin // 10) * 4
        shift = (pin % 10) * 3
        val = struct.unpack_from("<I", self._mem, reg)[0]
        val &= ~(0b111 << shift)      # clear existing mode
        val |= (0b001 << shift)       # output mode
        struct.pack_into("<I", self._mem, reg, val)

    def set_mask(self, mask: int) -> None:
        struct.pack_into("<I", self._mem, self._GPSET0, mask)

    def clear_mask(self, mask: int) -> None:
        struct.pack_into("<I", self._mem, self._GPCLR0, mask)

    def close(self) -> None:
        mem = getattr(self, "_mem", None)
        if mem is not None:
            mem.close()


# ---------------------------------------------------------------------------
# ILI9341 8-bit parallel driver (write-only, 8080-I style)
# ---------------------------------------------------------------------------

class ILI9341Parallel:
    WIDTH  = 240
    HEIGHT = 320

    _CMD_SLPOUT  = 0x11
    _CMD_COLMOD  = 0x3A
    _CMD_MADCTL  = 0x36
    _CMD_CASET   = 0x2A
    _CMD_RASET   = 0x2B
    _CMD_RAMWR   = 0x2C
    _CMD_DISPON  = 0x29

    def __init__(self, cfg) -> None:
        self._gpio = _FastGPIO()
        self._rst  = cfg.rst_pin
        self._cs   = cfg.cs_pin
        self._rs   = cfg.rs_pin
        self._wr   = cfg.wr_pin
        self._data = list(cfg.data_pins)   # [D0..D7] in order

        # Configure all pins as outputs
        for pin in [self._rst, self._cs, self._rs, self._wr] + self._data:
            self._gpio.setup_output(pin)

        # Pre-compute set/clear masks for every possible byte (0-255).
        # Each byte write reduces to 4 register writes: clear data lows,
        # set data highs, WR low, WR high.
        data_mask = 0
        for pin in self._data:
            data_mask |= 1 << pin
        self._data_mask   = data_mask
        self._set_masks   = [0] * 256
        self._clear_masks = [0] * 256
        for b in range(256):
            s = 0
            for bit, pin in enumerate(self._data):
                if (b >> bit) & 1:
                    s |= 1 << pin
            self._set_masks[b]   = s
            self._clear_masks[b] = data_mask & ~s

        self._rst_bit = 1 << self._rst
        self._cs_bit  = 1 << self._cs
        self._rs_bit  = 1 << self._rs
        self._wr_bit  = 1 << self._wr

        # Idle: CS high, WR high, RS high, RST high
        self._gpio.set_mask(self._cs_bit | self._wr_bit | self._rs_bit | self._rst_bit)

        self._reset()
        self._init()

    # -- low-level pin toggling ----------------------------------------------

    def _reset(self) -> None:
        g = self._gpio
        g.set_mask(self._rst_bit);   time.sleep(0.010)
        g.clear_mask(self._rst_bit); time.sleep(0.020)
        g.set_mask(self._rst_bit);   time.sleep(0.150)

    def _write_byte(self, byte: int) -> None:
        g = self._gpio
        g.clear_mask(self._clear_masks[byte])
        g.set_mask(self._set_masks[byte])
        g.clear_mask(self._wr_bit)
        g.set_mask(self._wr_bit)

    def _cmd(self, cmd: int) -> None:
        g = self._gpio
        g.clear_mask(self._cs_bit | self._rs_bit)   # CS low, RS low (command)
        self._write_byte(cmd)
        g.set_mask(self._cs_bit)

    def _data_bytes(self, *args: int) -> None:
        g = self._gpio
        g.clear_mask(self._cs_bit)
        g.set_mask(self._rs_bit)                    # RS high (data)
        for b in args:
            self._write_byte(b)
        g.set_mask(self._cs_bit)

    # -- init sequence -------------------------------------------------------

    def _init(self) -> None:
        # Adafruit-standard ILI9341 power-on sequence.
        self._cmd(0xEF); self._data_bytes(0x03, 0x80, 0x02)
        self._cmd(0xCF); self._data_bytes(0x00, 0xC1, 0x30)
        self._cmd(0xED); self._data_bytes(0x64, 0x03, 0x12, 0x81)
        self._cmd(0xE8); self._data_bytes(0x85, 0x00, 0x78)
        self._cmd(0xCB); self._data_bytes(0x39, 0x2C, 0x00, 0x34, 0x02)
        self._cmd(0xF7); self._data_bytes(0x20)
        self._cmd(0xEA); self._data_bytes(0x00, 0x00)
        self._cmd(0xC0); self._data_bytes(0x23)          # Power Control 1
        self._cmd(0xC1); self._data_bytes(0x10)          # Power Control 2
        self._cmd(0xC5); self._data_bytes(0x3E, 0x28)    # VCOM 1
        self._cmd(0xC7); self._data_bytes(0x86)          # VCOM 2
        self._cmd(self._CMD_MADCTL); self._data_bytes(0x48)  # portrait, BGR
        self._cmd(self._CMD_COLMOD); self._data_bytes(0x55)  # 16-bit RGB565
        self._cmd(0xB1); self._data_bytes(0x00, 0x18)    # frame rate 79 Hz
        self._cmd(0xB6); self._data_bytes(0x08, 0x82, 0x27)
        self._cmd(0xF2); self._data_bytes(0x00)          # 3-gamma off
        self._cmd(0x26); self._data_bytes(0x01)          # gamma set
        self._cmd(0xE0); self._data_bytes(               # positive gamma
            0x0F, 0x31, 0x2B, 0x0C, 0x0E, 0x08,
            0x4E, 0xF1, 0x37, 0x07, 0x10, 0x03,
            0x0E, 0x09, 0x00)
        self._cmd(0xE1); self._data_bytes(               # negative gamma
            0x00, 0x0E, 0x14, 0x03, 0x11, 0x07,
            0x31, 0xC1, 0x48, 0x08, 0x0F, 0x0C,
            0x31, 0x36, 0x0F)
        self._cmd(self._CMD_SLPOUT); time.sleep(0.12)
        self._cmd(self._CMD_DISPON)

    # -- public API ----------------------------------------------------------

    def _set_window(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self._cmd(self._CMD_CASET)
        self._data_bytes(x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF)
        self._cmd(self._CMD_RASET)
        self._data_bytes(y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF)
        self._cmd(self._CMD_RAMWR)

    def display(self, image) -> None:
        """Blit an image (≤WIDTH×HEIGHT PIL RGB) to (0,0)."""
        w, h = image.size
        if w > self.WIDTH or h > self.HEIGHT:
            raise ValueError(f"image {w}x{h} exceeds display {self.WIDTH}x{self.HEIGHT}")

        self._set_window(0, 0, w - 1, h - 1)

        img_rgb = image.convert("RGB")
        raw = img_rgb.tobytes()      # R,G,B,R,G,B,...

        g = self._gpio
        g.clear_mask(self._cs_bit)
        g.set_mask(self._rs_bit)

        # Hot loop — hoist all attribute lookups into locals
        mem         = g._mem
        pack        = struct.pack_into
        GPSET       = _FastGPIO._GPSET0
        GPCLR       = _FastGPIO._GPCLR0
        set_masks   = self._set_masks
        clear_masks = self._clear_masks
        wr_bit      = self._wr_bit

        fmt = "<I"
        n = len(raw)
        i = 0
        while i < n:
            r  = raw[i]
            gr = raw[i + 1]
            b  = raw[i + 2]
            i += 3

            hi = (r & 0xF8) | (gr >> 5)
            lo = ((gr & 0x1C) << 3) | (b >> 3)

            # high byte
            pack(fmt, mem, GPCLR, clear_masks[hi])
            pack(fmt, mem, GPSET, set_masks[hi])
            pack(fmt, mem, GPCLR, wr_bit)
            pack(fmt, mem, GPSET, wr_bit)

            # low byte
            pack(fmt, mem, GPCLR, clear_masks[lo])
            pack(fmt, mem, GPSET, set_masks[lo])
            pack(fmt, mem, GPCLR, wr_bit)
            pack(fmt, mem, GPSET, wr_bit)

        g.set_mask(self._cs_bit)

    def fill(self, r: int, g: int, b: int) -> None:
        """Flood the entire screen with a single RGB colour."""
        hi = (r & 0xF8) | (g >> 5)
        lo = ((g & 0x1C) << 3) | (b >> 3)
        self._set_window(0, 0, self.WIDTH - 1, self.HEIGHT - 1)

        gp = self._gpio
        gp.clear_mask(self._cs_bit)
        gp.set_mask(self._rs_bit)

        mem   = gp._mem
        pack  = struct.pack_into
        GPSET = _FastGPIO._GPSET0
        GPCLR = _FastGPIO._GPCLR0
        s_hi, c_hi = self._set_masks[hi], self._clear_masks[hi]
        s_lo, c_lo = self._set_masks[lo], self._clear_masks[lo]
        wr = self._wr_bit

        for _ in range(self.WIDTH * self.HEIGHT):
            pack("<I", mem, GPCLR, c_hi); pack("<I", mem, GPSET, s_hi)
            pack("<I", mem, GPCLR, wr);   pack("<I", mem, GPSET, wr)
            pack("<I", mem, GPCLR, c_lo); pack("<I", mem, GPSET, s_lo)
            pack("<I", mem, GPCLR, wr);   pack("<I", mem, GPSET, wr)

        gp.set_mask(self._cs_bit)

    def close(self) -> None:
        try:
            self._gpio.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Rendering (identical concept to webui: top-down radar with zones + targets)
# ---------------------------------------------------------------------------

def _load_font(size: int):
    from PIL import ImageFont
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _mm_to_px(x_mm: float, y_mm: float) -> tuple[int, int]:
    """Map sensor mm coords to radar-area pixel coords."""
    px = int((x_mm - RADAR_X_MIN) / (RADAR_X_MAX - RADAR_X_MIN) * RADAR_W)
    py = HEADER_H + RADAR_H - 1 - int(
        (y_mm - RADAR_Y_MIN) / (RADAR_Y_MAX - RADAR_Y_MIN) * RADAR_H
    )
    return (
        max(0, min(RADAR_W - 1, px)),
        max(HEADER_H, min(HEADER_H + RADAR_H - 1, py)),
    )


def _render_frame(state: DisplayState, cfg: AppConfig):
    from PIL import Image, ImageDraw

    img     = Image.new("RGB", (240, 320), C_STATUS_BG)
    draw    = ImageDraw.Draw(img)
    font_sm = _load_font(11)
    font_md = _load_font(14)
    font_xs = _load_font(9)

    # ── Header ──────────────────────────────────────────────────────────
    hdr_color = C_HDR_PRESENT if state.presence else C_HDR_ABSENT
    draw.rectangle([(0, 0), (239, HEADER_H - 1)], fill=hdr_color)
    draw.text((6, 6), f"Personen: {state.count}", font=font_md, fill=C_WHITE)
    if state.co2_alert:
        draw.text((170, 6), "CO2!", font=font_md, fill=C_ORANGE)

    # ── Radar background + range rings ──────────────────────────────────
    draw.rectangle(
        [(0, HEADER_H), (RADAR_W - 1, HEADER_H + RADAR_H - 1)],
        fill=C_BG_RADAR,
    )
    for r_mm in (1000, 2000, 3000, 4000):
        _, py = _mm_to_px(0, r_mm)
        draw.line([(0, py), (RADAR_W - 1, py)], fill=C_GRID)
        draw.text((2, py - 10), f"{r_mm // 1000}m", font=font_xs, fill=C_GRID)
    cx, _ = _mm_to_px(0, 0)
    draw.line([(cx, HEADER_H), (cx, HEADER_H + RADAR_H - 1)], fill=C_GRID)

    # ── Zones ───────────────────────────────────────────────────────────
    for zone in cfg.zones:
        x0, y0 = _mm_to_px(zone.x_min, zone.y_max)
        x1, y1 = _mm_to_px(zone.x_max, zone.y_min)
        if x1 < x0: x0, x1 = x1, x0
        if y1 < y0: y0, y1 = y1, y0
        occupied = state.zone_occupied.get(zone.name, False)
        if occupied:
            draw.rectangle([(x0, y0), (x1, y1)],
                           fill=C_ZONE_FILL, outline=C_ZONE_OCC, width=2)
        else:
            draw.rectangle([(x0, y0), (x1, y1)], outline=C_ZONE_EMPTY, width=1)
        label = zone.name[:6]
        lw = len(label) * 6
        draw.text(((x0 + x1 - lw) // 2, (y0 + y1) // 2 - 6),
                  label, font=font_sm, fill=C_WHITE)

    # ── Targets ─────────────────────────────────────────────────────────
    for t in state.targets:
        if t.get("x") is not None and t.get("y") is not None:
            tx, ty = _mm_to_px(t["x"], t["y"])
            draw.ellipse([(tx - 5, ty - 5), (tx + 5, ty + 5)], fill=C_TARGET)

    # Sensor indicator
    sx, _ = _mm_to_px(0, 0)
    sy = HEADER_H + RADAR_H - 1
    draw.polygon([(sx, sy - 10), (sx - 7, sy), (sx + 7, sy)], fill=C_SENSOR)

    # ── Status bar ──────────────────────────────────────────────────────
    sy0 = HEADER_H + RADAR_H
    draw.rectangle([(0, sy0), (239, 319)], fill=C_STATUS_BG)
    draw.text((6, sy0 + 4),  f"IP: {get_ip_address()}", font=font_sm, fill=C_GREY)
    draw.text((6, sy0 + 20), f"Up: {get_uptime()}",     font=font_sm, fill=C_GREY)
    alert_text  = "CO2: ALARM!" if state.co2_alert else "CO2: OK"
    alert_color = C_ORANGE if state.co2_alert else C_GREEN
    draw.text((6, sy0 + 36), alert_text, font=font_sm, fill=alert_color)

    return img


def _demo_render(state: DisplayState, cfg: AppConfig) -> None:
    zones_str = ", ".join(
        f"{z.name}={'ON' if state.zone_occupied.get(z.name) else 'off'}"
        for z in cfg.zones
    )
    active = [t for t in state.targets if t.get("x") is not None]
    logger.info(
        f"[RENDER] Personen:{state.count} {'ANWESEND' if state.presence else 'leer'} | "
        f"Zonen:[{zones_str}] | Targets:{len(active)} | "
        f"CO2:{'ALARM' if state.co2_alert else 'ok'}"
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
                        state.zone_occupied[topic.split("/")[-1]] = payload == "true"
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
                # GPIO writes are blocking but fast (~100-250 ms); run in thread.
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
        logger.info("ILI9341 TFT initialised (240×320, 8-bit parallel, mmap GPIO)")
        # Brief startup flash so the user can see the display responded to init.
        await asyncio.to_thread(device.fill, 0, 80, 0)   # green flash
        await asyncio.sleep(0.2)
    except Exception as e:
        logger.warning(f"No TFT hardware ({e}) — rendering to stdout")

    try:
        await asyncio.gather(
            _mqtt_listener(cfg, state),
            _render_loop(cfg, state, device),
        )
    finally:
        if device is not None:
            device.close()


if __name__ == "__main__":
    asyncio.run(main())
