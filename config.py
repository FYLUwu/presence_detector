"""
Config loader and validator for the presence detection system.
Reads config.json and exposes a typed AppConfig dataclass tree.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class ConfigError(Exception):
    pass


@dataclass
class MqttConfig:
    broker: str
    port: int
    keepalive: int
    username: Optional[str]
    password: Optional[str]

    def __post_init__(self) -> None:
        if not self.broker:
            raise ConfigError("mqtt.broker must not be empty")
        if not (1 <= self.port <= 65535):
            raise ConfigError(f"mqtt.port must be 1-65535, got {self.port}")
        if self.keepalive <= 0:
            raise ConfigError(f"mqtt.keepalive must be positive, got {self.keepalive}")


@dataclass
class SensorConfig:
    port: str
    baudrate: int
    timeout: float

    def __post_init__(self) -> None:
        if not self.port:
            raise ConfigError("sensor.port must not be empty")
        if self.baudrate <= 0:
            raise ConfigError(f"sensor.baudrate must be positive, got {self.baudrate}")
        if self.timeout <= 0:
            raise ConfigError(f"sensor.timeout must be positive, got {self.timeout}")


@dataclass
class Co2Config:
    port: str
    baudrate: int
    use_gpio: bool
    gpio_pin: int
    read_interval_seconds: int
    threshold_ppm: int

    def __post_init__(self) -> None:
        if not self.port:
            raise ConfigError("co2.port must not be empty")
        if self.read_interval_seconds <= 0:
            raise ConfigError(f"co2.read_interval_seconds must be positive, got {self.read_interval_seconds}")
        if self.threshold_ppm <= 0:
            raise ConfigError(f"co2.threshold_ppm must be positive, got {self.threshold_ppm}")


@dataclass
class GpioConfig:
    presence_pin: int
    alert_pin: int

    def __post_init__(self) -> None:
        for name, pin in (("presence_pin", self.presence_pin), ("alert_pin", self.alert_pin)):
            if not (1 <= pin <= 40):
                raise ConfigError(f"gpio.{name} must be 1-40, got {pin}")


@dataclass
class DisplayConfig:
    i2c_address: str
    width: int
    height: int
    font_size: int

    def __post_init__(self) -> None:
        if not self.i2c_address.startswith("0x"):
            raise ConfigError(f"display.i2c_address must be hex (e.g. '0x3C'), got '{self.i2c_address}'")
        if self.width <= 0 or self.height <= 0:
            raise ConfigError("display.width and height must be positive")


@dataclass
class ZoneConfig:
    name: str
    x_min: int
    x_max: int
    y_min: int
    y_max: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigError("zone.name must not be empty")
        if self.x_min >= self.x_max:
            raise ConfigError(f"zone '{self.name}': x_min must be < x_max")
        if self.y_min >= self.y_max:
            raise ConfigError(f"zone '{self.name}': y_min must be < y_max")


@dataclass
class WatchdogConfig:
    modules: list[str]
    check_interval_seconds: int
    restart_delay_seconds: int

    def __post_init__(self) -> None:
        if not self.modules:
            raise ConfigError("watchdog.modules must not be empty")
        if self.check_interval_seconds <= 0:
            raise ConfigError(f"watchdog.check_interval_seconds must be positive")
        if self.restart_delay_seconds < 0:
            raise ConfigError(f"watchdog.restart_delay_seconds must be >= 0")


@dataclass
class AppConfig:
    mqtt: MqttConfig
    sensor: SensorConfig
    co2: Co2Config
    gpio: GpioConfig
    display: DisplayConfig
    zones: list[ZoneConfig]
    watchdog: WatchdogConfig


def load_config(path: str = "config.json") -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {path}")

    try:
        raw = json.loads(config_path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in {path}: {e}") from e

    try:
        return AppConfig(
            mqtt=MqttConfig(**raw["mqtt"]),
            sensor=SensorConfig(**raw["sensor"]),
            co2=Co2Config(**raw["co2"]),
            gpio=GpioConfig(**raw["gpio"]),
            display=DisplayConfig(**raw["display"]),
            zones=[ZoneConfig(**z) for z in raw.get("zones", [])],
            watchdog=WatchdogConfig(**raw["watchdog"]),
        )
    except KeyError as e:
        raise ConfigError(f"Missing required config key: {e}") from e
    except TypeError as e:
        raise ConfigError(f"Config structure error: {e}") from e
