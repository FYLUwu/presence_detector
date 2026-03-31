"""
Watchdog Module — supervises all other modules and restarts crashed ones.

Launches each configured module as a child subprocess and monitors its
exit code. On unexpected exit, waits restart_delay_seconds then relaunches.

Publishes health status to:
  system/health/<module_name>
  payload: {"status": "running"|"restarting"|"failed", "pid": <int>, "restart_count": <int>}

The watchdog does not supervise itself — it is the root process and
should be launched by systemd or run directly.

Usage:
    python watchdog_module.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

import aiomqtt

from config import AppConfig, load_config
from utils import setup_logging

logger = setup_logging("watchdog")

RECONNECT_DELAY = 5.0


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

async def _start_module(name: str) -> asyncio.subprocess.Process:
    module_path = Path(__file__).parent / f"{name}.py"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(module_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    logger.info(f"Started {name} (pid={proc.pid})")
    return proc


# ---------------------------------------------------------------------------
# Monitor loop
# ---------------------------------------------------------------------------

async def _monitor_loop(
    cfg: AppConfig,
    processes: dict[str, asyncio.subprocess.Process],
    restart_counts: defaultdict,
    status: dict[str, str],
) -> None:
    # Initial launch of all modules
    for name in cfg.watchdog.modules:
        processes[name] = await _start_module(name)
        status[name] = "running"

    while True:
        await asyncio.sleep(cfg.watchdog.check_interval_seconds)
        for name in list(processes.keys()):
            proc = processes[name]
            if proc.returncode is not None:
                logger.warning(
                    f"{name} exited with code {proc.returncode} — "
                    f"restarting in {cfg.watchdog.restart_delay_seconds}s"
                )
                status[name] = "restarting"
                await asyncio.sleep(cfg.watchdog.restart_delay_seconds)
                try:
                    processes[name] = await _start_module(name)
                    restart_counts[name] += 1
                    status[name] = "running"
                except Exception as e:
                    logger.error(f"Failed to restart {name}: {e}")
                    status[name] = "failed"


# ---------------------------------------------------------------------------
# Health publisher
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


async def _health_publisher(
    cfg: AppConfig,
    processes: dict[str, asyncio.subprocess.Process],
    restart_counts: defaultdict,
    status: dict[str, str],
) -> None:
    while True:
        try:
            async with _build_client(cfg) as client:
                logger.info("Health publisher connected to MQTT broker")
                while True:
                    for name, proc in list(processes.items()):
                        payload = json.dumps({
                            "status": status.get(name, "unknown"),
                            "pid": proc.pid,
                            "restart_count": restart_counts[name],
                        })
                        await client.publish(
                            f"system/health/{name}",
                            payload,
                            retain=True,
                        )
                    await asyncio.sleep(cfg.watchdog.check_interval_seconds)

        except aiomqtt.MqttError as e:
            logger.warning(f"MQTT error: {e} — reconnecting in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)
        except Exception as e:
            logger.error(f"Health publisher error: {e} — reconnecting in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    cfg = load_config()

    processes: dict[str, asyncio.subprocess.Process] = {}
    restart_counts: defaultdict = defaultdict(int)
    status: dict[str, str] = {}

    try:
        await asyncio.gather(
            _monitor_loop(cfg, processes, restart_counts, status),
            _health_publisher(cfg, processes, restart_counts, status),
        )
    finally:
        logger.info("Watchdog shutting down — terminating child processes")
        for name, proc in processes.items():
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    logger.warning(f"Force-killed {name} (pid={proc.pid})")


if __name__ == "__main__":
    asyncio.run(main())
