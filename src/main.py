"""Entry point: python -m src.main [--config config.yaml]"""

import argparse
import logging
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
import yaml

from .api_server import create_app
from .controller import Controller, Device
from .relays import Relay, cleanup_gpio, init_gpio
from .schedules import build_schedule

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_FILE = BASE_DIR / "data" / "logs" / "timer.log"

logger = logging.getLogger("timer")


def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
        ],
    )


def load_env(path: Path):
    """Minimal .env reader (KEY=value lines) so we don't need python-dotenv."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_devices(config: dict) -> list[Device]:
    active_low = config.get("relay", {}).get("active_low", True)
    devices_cfg = config.get("devices") or {}
    if not devices_cfg:
        raise ValueError("config.yaml has no 'devices' section")

    pins_used = {}
    devices = []
    for name, cfg in devices_cfg.items():
        if not cfg.get("enabled", True):
            logger.info("%s: disabled in config, skipping", name)
            continue
        pin = cfg.get("pin")
        if pin is None:
            raise ValueError(f"{name}: missing 'pin'")
        if pin in pins_used:
            raise ValueError(f"{name}: pin {pin} is already used by {pins_used[pin]}")
        pins_used[pin] = name

        try:
            schedule = build_schedule(cfg.get("schedule"))
        except ValueError as e:
            raise ValueError(f"{name}: {e}")

        max_run = cfg.get("max_run_seconds")
        run_seconds = getattr(schedule, "run_seconds", None)
        if max_run and run_seconds and run_seconds > max_run:
            raise ValueError(f"{name}: run_seconds ({run_seconds:g}) is more than "
                             f"max_run_seconds ({max_run:g})")

        relay = Relay(name, pin, active_low=active_low)
        devices.append(Device(name, relay, schedule, max_run_seconds=max_run))
    return devices


def _exit_on_sigterm(signum, frame):
    raise SystemExit(0)


def main():
    parser = argparse.ArgumentParser(description="VIP Vertical Farm timer controller")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    args = parser.parse_args()

    setup_logging()
    load_env(BASE_DIR / ".env")

    with open(args.config) as f:
        config = yaml.safe_load(f) or {}

    init_gpio()
    try:
        devices = build_devices(config)
    except ValueError as e:
        logger.error("config error: %s", e)
        sys.exit(1)

    controller = Controller(devices)
    loop = threading.Thread(target=controller.run, daemon=True)
    loop.start()

    api_cfg = config.get("api", {})
    host, port = api_cfg.get("host", "0.0.0.0"), api_cfg.get("port", 8080)
    app = create_app(controller, api_key=os.environ.get("API_KEY") or None)
    logger.info("API at http://%s:%d/api/status", host, port)

    # uvicorn re-raises SIGTERM (systemctl stop) after it shuts down. Without a
    # handler that kills the process before `finally` runs and the relays
    # could stay ON - so turn it into a normal exit.
    signal.signal(signal.SIGTERM, _exit_on_sigterm)

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        logger.info("shutting down - turning everything OFF")
        controller.stop()
        loop.join(timeout=5)
        cleanup_gpio([d.relay for d in devices])


if __name__ == "__main__":
    main()
