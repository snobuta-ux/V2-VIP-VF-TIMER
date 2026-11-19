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

from .api_server import create_app
from .camera import build_cameras
from .cloud_log import CloudLogHandler, FirebaseLogger
from .config import build_devices, load_config  # noqa: F401  (build_devices re-exported for tests)
from .controller import Controller
from .relays import cleanup_gpio, init_gpio
from .remote import RemoteControl

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


def _exit_on_sigterm(signum, frame):
    raise SystemExit(0)


def main():
    parser = argparse.ArgumentParser(description="VIP Vertical Farm timer controller")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    args = parser.parse_args()

    setup_logging()
    load_env(BASE_DIR / ".env")

    # config.yaml + config.local.yaml (edits from the website)
    try:
        config = load_config(BASE_DIR, Path(args.config))
    except Exception as e:
        logger.error("config error: %s", e)
        sys.exit(1)

    init_gpio()
    try:
        devices = build_devices(config)
    except ValueError as e:
        logger.error("config error: %s", e)
        sys.exit(1)

    controller = Controller(devices)
    remote = None
    cameras = {}

    def status():
        st = controller.status()
        if remote is not None:
            try:
                st["remote"] = remote.status()
            except Exception as e:
                st["remote"] = {"error": str(e)}
        return st

    # Firebase logging starts before the timer loop so the first switches are sent too.
    farm_info = {
        "name": config.get("firebase", {}).get("farm_name", "V2 timer farm"),
        "devices": {d.name: {"pin": d.relay.pin, "schedule": d.schedule.describe(),
                             "max_run_seconds": d.max_run_seconds} for d in devices},
    }
    fb = FirebaseLogger(config.get("firebase"), BASE_DIR, status, farm_info)
    if fb.start():
        logging.getLogger().addHandler(CloudLogHandler(fb))
        controller.listeners.append(fb.controller_event)

    # Webcams (plant + dashboard). They must never stop the timer.
    light = controller.devices.get("light")
    try:
        built = build_cameras(config, BASE_DIR, fb,
                              light_is_on=(lambda: light.relay.is_on) if light else None)
    except Exception as e:
        logger.warning("camera: NOT taking photos (%s). The timer still runs.", e)
        built = {}
    for name, cam in built.items():
        try:
            if cam.start():
                cameras[name] = cam
        except Exception as e:
            logger.warning("camera %s: NOT taking photos (%s). The timer still runs.", name, e)

    loop = threading.Thread(target=controller.run, daemon=True)
    loop.start()

    # Website remote control (through Firestore). A restart request makes
    # uvicorn shut down like `systemctl stop` would (so the relays go OFF),
    # then the process exits with `exit_code` and supervisor.py starts it again.
    exit_code = {"value": 0}

    def restart(code):
        exit_code["value"] = code
        logger.info("restart requested from the website")
        os.kill(os.getpid(), signal.SIGTERM)

    remote = RemoteControl(controller, fb, cameras, config, BASE_DIR, restart_fn=restart)
    try:
        remote.start()
    except Exception as e:
        logger.warning("remote: website control NOT available (%s). The timer still runs.", e)

    api_cfg = config.get("api", {})
    host, port = api_cfg.get("host", "0.0.0.0"), api_cfg.get("port", 8080)
    app = create_app(controller, api_key=os.environ.get("API_KEY") or None, cameras=cameras)
    logger.info("API at http://%s:%d/api/status", host, port)

    # uvicorn re-raises SIGTERM (systemctl stop) after it shuts down. Without a
    # handler that kills the process before `finally` runs and the relays
    # could stay ON - so turn it into a normal exit.
    signal.signal(signal.SIGTERM, _exit_on_sigterm)

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        logger.info("shutting down - turning everything OFF")
        remote.stop()
        controller.stop()
        loop.join(timeout=5)
        cleanup_gpio([d.relay for d in devices])
        for cam in cameras.values():
            cam.stop()
        fb.stop()
        if exit_code["value"]:
            logging.shutdown()
            os._exit(exit_code["value"])


if __name__ == "__main__":
    main()
