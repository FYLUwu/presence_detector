# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run all modules directly (color-coded output, web UI at http://localhost:8080)
python run.py

# Run with watchdog managing module restarts instead
python run.py --watchdog

# Run a single module independently
python sensor_module.py
python mqtt_module.py
python co2_module.py
python display_module.py
python gpio_module.py
python webui_module.py
python watchdog_module.py
```

## Architecture

This is a Raspberry Pi presence detection system built around the HLK-LD2450 millimeter-wave radar sensor. Every module is a standalone async Python process; they communicate via MQTT and a shared JSON file.

### Data flow

```
sensor_module → /tmp/sensor_stream.json → mqtt_module → MQTT broker
                                        ↗                      ↓
co2_module ─────────────────────────────→ MQTT broker   gpio_module
                                                        display_module
                                                        webui_module
```

1. **sensor_module** reads binary frames from the HLK-LD2450 over UART (`/dev/ttyUSB0`), parses up to 3 targets, and writes `{targets, timestamp}` atomically to `/tmp/sensor_stream.json` at ~10 Hz.
2. **mqtt_module** reads that file and publishes to MQTT at 2 Hz: per-target JSON, aggregate presence, count, and zone occupancy (`sensor/HLK-LD2450/*`).
3. **co2_module** subscribes to `/sensor/bme680/gas` (published by the BME680 sensor) and publishes only `sensor/CO2/alert` when the configured ppm threshold is exceeded. It does not re-publish the raw ppm value. Falls back to Ornstein-Uhlenbeck demo values if the broker is offline.
4. **gpio_module** subscribes to `sensor/HLK-LD2450/presence` and `sensor/CO2/alert` and drives BCM GPIO output pins accordingly.
5. **display_module** subscribes to those MQTT topics and renders a 4-line status screen on an SSD1306 OLED (128×64) at 1 Hz.
6. **webui_module** serves a FastAPI app on port 8080 with a live WebSocket radar view (`/ws`) and zone editing (`PUT /api/zones`).
7. **watchdog_module** (used with `--watchdog`) spawns all modules as subprocesses, monitors exit codes, and restarts crashed ones, publishing health status to `system/health/<module_name>`.

### Demo mode

Every hardware-dependent module calls `is_demo_mode()` from `utils.py` during startup. If the required resource (serial port, MQTT broker, I2C device, RPi.GPIO) raises any exception, the module falls back to synthetic data generation — sinusoidal targets, Ornstein-Uhlenbeck CO2 drift, stdout rendering — so the system works on a development machine without hardware.

### Configuration

All configuration lives in `config.json` (loaded via `config.py` into typed dataclasses). The web UI can edit zones at runtime via `PUT /api/zones`, which writes back to `config.json` directly. The watchdog module reads zone config on every broadcast cycle to pick up changes without restart.

### Key files

| File | Purpose |
|---|---|
| `config.py` | Typed `AppConfig` dataclass tree; loaded by every module |
| `utils.py` | `is_demo_mode`, atomic file I/O, zone detection, logging setup |
| `static/index.html` | Single-file web UI (served by webui_module) |
| `config.json` | Runtime configuration; edited by web UI for zones |

### MQTT topic map

| Topic | Publisher | Payload |
|---|---|---|
| `sensor/HLK-LD2450/target_<n>` | mqtt_module | JSON object or `{}` |
| `sensor/HLK-LD2450/targets` | mqtt_module | JSON array |
| `sensor/HLK-LD2450/presence` | mqtt_module | `"true"` / `"false"` |
| `sensor/HLK-LD2450/count` | mqtt_module | integer string |
| `sensor/HLK-LD2450/zone/<name>` | mqtt_module | `"true"` / `"false"` |
| `/sensor/bme680/gas` | BME680 (external) | integer ppm string |
| `sensor/CO2/alert` | co2_module | `"true"` / `"false"` |
| `system/health/<module>` | watchdog_module | `{status, pid, restart_count}` |

### HLK-LD2450 frame format

Frames are 30 bytes: 4-byte header (`AA FF 03 00`), three 8-byte target slots, 2-byte footer (`55 CC`). Coordinates use a non-standard sign encoding handled by `_decode_coord` in `sensor_module.py`: if the signed int16 is negative, the true value is `-32768 - raw`. Y is negated so positive values = distance in front of the sensor. Empty slots are all-zero bytes.

### RPi.GPIO dependency

`RPi.GPIO` is conditionally installed only on ARM targets (see `requirements.txt`). On x86 dev machines it will be absent, triggering demo mode in `gpio_module`.
