"""
Launcher — starts all modules simultaneously with color-coded, prefixed output.

Usage:
    python run.py              # start all modules
    python run.py --watchdog   # let the watchdog manage modules instead
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

# ANSI colors (one per module)
_COLORS = ["\033[92m", "\033[94m", "\033[96m", "\033[93m", "\033[95m", "\033[91m"]
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"

MODULES = [
    "sensor_module",
    "mqtt_module",
    "co2_module",
    "display_module",
    "gpio_module",
    "webui_module",
]

processes: list[asyncio.subprocess.Process] = []


async def stream_output(
    stream: asyncio.StreamReader,
    prefix: str,
    color: str,
    is_stderr: bool,
) -> None:
    label_w = max(len(m) for m in MODULES) + 2
    label = f"{color}{_BOLD}{prefix:<{label_w}}{_RESET}"
    sep   = f"{_DIM}│{_RESET} "
    err   = f"{_DIM}ERR{_RESET} " if is_stderr else ""
    while True:
        line = await stream.readline()
        if not line:
            break
        print(f"{label}{sep}{err}{line.decode(errors='replace').rstrip()}", flush=True)


async def run_module(name: str, color: str) -> None:
    path = Path(__file__).parent / f"{name}.py"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    processes.append(proc)
    await asyncio.gather(
        stream_output(proc.stdout, name, color, False),
        stream_output(proc.stderr, name, color, True),
    )
    rc = await proc.wait()
    print(f"{color}{_BOLD}{name}{_RESET} exited with code {rc}", flush=True)


async def run_watchdog() -> None:
    color = _COLORS[0]
    path  = Path(__file__).parent / "watchdog_module.py"
    proc  = await asyncio.create_subprocess_exec(
        sys.executable, str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    processes.append(proc)
    await asyncio.gather(
        stream_output(proc.stdout, "watchdog", color, False),
        stream_output(proc.stderr, "watchdog", color, True),
    )


def shutdown(*_) -> None:
    print(f"\n{_DIM}Shutting down…{_RESET}", flush=True)
    for p in processes:
        if p.returncode is None:
            p.terminate()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchdog", action="store_true",
                        help="start watchdog_module instead of each module directly")
    args = parser.parse_args()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)

    print(f"{_BOLD}Presence Detector{_RESET}", flush=True)
    print(f"{_DIM}{'─' * 60}{_RESET}", flush=True)

    if args.watchdog:
        print(f"Mode: watchdog\n", flush=True)
        await run_watchdog()
    else:
        print(f"Mode: direct  │  Web UI → http://localhost:8080\n", flush=True)
        await asyncio.gather(*(
            run_module(name, _COLORS[i % len(_COLORS)])
            for i, name in enumerate(MODULES)
        ))


if __name__ == "__main__":
    asyncio.run(main())
