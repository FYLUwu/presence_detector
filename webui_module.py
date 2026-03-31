"""
Web UI Module — live radar interface served over HTTP/WebSocket.

Endpoints:
  GET  /              → serves static/index.html
  GET  /ws            → WebSocket, pushes sensor+zone+CO2 state at 2 Hz
  PUT  /api/zones     → persist zone edits to config.json
  GET  /api/config    → return current config (zones, thresholds)

Data flow:
  /tmp/sensor_stream.json → targets
  MQTT sensor/CO2/*       → CO2 state (falls back to demo values)
  config.json             → zones (editable via PUT /api/zones)

Usage:
    python webui_module.py
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import socket
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import ZoneConfig, load_config
from utils import STREAM_PATH, is_demo_mode, read_stream_file, setup_logging

logger = setup_logging("webui")

CONFIG_PATH = Path("config.json")
STATIC_DIR = Path(__file__).parent / "static"
BROADCAST_INTERVAL = 0.5

app = FastAPI(title="Presence Radar")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_co2_state: dict[str, Any] = {"ppm": 0, "alert": False}
_connections: set[WebSocket] = set()
_demo_t: float = 0.0
_demo_co2: float = 650.0


# ---------------------------------------------------------------------------
# Demo data helpers
# ---------------------------------------------------------------------------

_DEMO_PARAMS = [
    (400, 500, 0.4, 0.0, 0.0, 800),
    (300, 600, 0.3, 1.0, 2.0, 2400),
    (250, 400, 0.5, 2.5, 1.0, 1600),
]


def _gen_demo_targets() -> list[dict]:
    global _demo_t
    targets = []
    for i, (ax, ay, omega, px, py, oy) in enumerate(_DEMO_PARAMS):
        x = int(ax * math.sin(omega * _demo_t + px))
        y = int(ay * math.sin(omega * _demo_t + py) + oy)
        targets.append({"id": i + 1, "x": x, "y": y})
    _demo_t += BROADCAST_INTERVAL
    return targets


def _gen_demo_co2(cfg) -> tuple[int, bool]:
    global _demo_co2
    _demo_co2 = max(400.0, min(1800.0, _demo_co2 + random.gauss(0, 15)))
    ppm = int(_demo_co2)
    return ppm, ppm > cfg.co2.threshold_ppm


# ---------------------------------------------------------------------------
# Zone occupancy check
# ---------------------------------------------------------------------------

def _zones_with_occupancy(zones: list[ZoneConfig], targets: list[dict]) -> list[dict]:
    result = []
    for z in zones:
        occupied = any(
            z.x_min <= t["x"] <= z.x_max and z.y_min <= t["y"] <= z.y_max
            for t in targets
        )
        result.append({
            "name": z.name,
            "x_min": z.x_min,
            "x_max": z.x_max,
            "y_min": z.y_min,
            "y_max": z.y_max,
            "occupied": occupied,
        })
    return result


# ---------------------------------------------------------------------------
# MQTT CO2 listener (background task)
# ---------------------------------------------------------------------------

async def _mqtt_co2_listener(cfg) -> None:
    try:
        import aiomqtt
    except ImportError:
        logger.warning("aiomqtt not installed — CO2 data will use demo values")
        return

    while True:
        try:
            async with aiomqtt.Client(
                hostname=cfg.mqtt.broker,
                port=cfg.mqtt.port,
                keepalive=cfg.mqtt.keepalive,
            ) as client:
                await client.subscribe("sensor/CO2/ppm")
                await client.subscribe("sensor/CO2/alert")
                async for msg in client.messages:
                    topic = str(msg.topic)
                    payload = msg.payload.decode(errors="replace").strip()
                    if topic == "sensor/CO2/ppm":
                        _co2_state["ppm"] = int(payload)
                    elif topic == "sensor/CO2/alert":
                        _co2_state["alert"] = payload == "true"
        except Exception as e:
            logger.debug(f"MQTT CO2 listener: {e} — retrying in 5s")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# WebSocket broadcast loop
# ---------------------------------------------------------------------------

async def _broadcast_loop(cfg, sensor_demo: bool) -> None:
    while True:
        await asyncio.sleep(BROADCAST_INTERVAL)
        if not _connections:
            continue

        if sensor_demo:
            targets = _gen_demo_targets()
            co2_ppm, co2_alert = _gen_demo_co2(cfg)
        else:
            data = read_stream_file(STREAM_PATH)
            targets = data["targets"] if data else []
            co2_ppm = _co2_state["ppm"]
            co2_alert = _co2_state["alert"]

        # Reload zones from config each cycle to reflect any saves
        try:
            live_cfg = load_config()
            zones = live_cfg.zones
        except Exception:
            zones = cfg.zones

        payload = json.dumps({
            "targets": targets,
            "zones": _zones_with_occupancy(zones, targets),
            "count": len(targets),
            "presence": len(targets) > 0,
            "co2_ppm": co2_ppm,
            "co2_alert": co2_alert,
            "timestamp": time.time(),
        })

        dead: set[WebSocket] = set()
        for ws in list(_connections):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        _connections.difference_update(dead)


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _connections.add(ws)
    logger.info(f"WebSocket connected ({len(_connections)} total)")
    try:
        while True:
            await ws.receive_text()  # keep alive / ignore client messages
    except WebSocketDisconnect:
        pass
    finally:
        _connections.discard(ws)
        logger.info(f"WebSocket disconnected ({len(_connections)} total)")


@app.put("/api/zones")
async def update_zones(zones: list[dict]):
    try:
        raw = json.loads(CONFIG_PATH.read_text())
        raw["zones"] = zones
        CONFIG_PATH.write_text(json.dumps(raw, indent=2))
        logger.info(f"Zones saved: {[z['name'] for z in zones]}")
        return {"ok": True, "count": len(zones)}
    except Exception as e:
        logger.error(f"Failed to save zones: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/config")
async def get_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    cfg = load_config()
    sensor_demo = not Path(STREAM_PATH).exists()
    mqtt_demo = is_demo_mode(
        lambda: socket.create_connection((cfg.mqtt.broker, cfg.mqtt.port), timeout=2)
    )
    if sensor_demo:
        logger.warning("DEMO MODE: no sensor stream found — generating synthetic targets")
    if mqtt_demo:
        logger.warning("DEMO MODE: MQTT broker unreachable — CO2 values will be simulated")

    asyncio.create_task(_broadcast_loop(cfg, sensor_demo))
    if not mqtt_demo:
        asyncio.create_task(_mqtt_co2_listener(cfg))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    STATIC_DIR.mkdir(exist_ok=True)
    config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="warning")
    server = uvicorn.Server(config)
    logger.info("Web UI available at http://0.0.0.0:8080")
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
