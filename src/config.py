"""Load and check the configuration.

config.yaml        tracked in git: the normal settings
config.local.yaml  NOT in git: overrides written from the website (e.g. which
                   webcam is which). Anything in it replaces the same key in
                   config.yaml, so a `git pull` never conflicts with it.
"""

from pathlib import Path
from typing import Callable, Optional

import yaml

from .relays import Relay
from .schedules import build_schedule

BASE_FILE = "config.yaml"
LOCAL_FILE = "config.local.yaml"


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a YAML mapping (key: value)")
    return data


def load_config(base_dir: Path, base_file: Optional[Path] = None) -> dict:
    base_file = base_file or Path(base_dir) / BASE_FILE
    return deep_merge(read_yaml(base_file), read_yaml(Path(base_dir) / LOCAL_FILE))


def build_devices(config: dict, make_relay: Optional[Callable] = None):
    """config -> list of Device. make_relay(name, pin, active_low) -> relay;
    pass `lambda *a: None` to only check the config without touching GPIO."""
    from .controller import Device  # avoid an import cycle

    make_relay = make_relay or (lambda name, pin, active_low: Relay(name, pin, active_low=active_low))
    active_low = (config.get("relay") or {}).get("active_low", True)
    devices_cfg = config.get("devices") or {}
    if not devices_cfg:
        raise ValueError("config.yaml has no 'devices' section")

    pins_used = {}
    devices = []
    for name, cfg in devices_cfg.items():
        cfg = cfg or {}
        if not cfg.get("enabled", True):
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

        devices.append(Device(name, make_relay(name, pin, active_low), schedule,
                              max_run_seconds=max_run))
    return devices


def validate_config(config) -> list[str]:
    """Problems that would stop the timer from starting ([] = OK). Touches no hardware."""
    if not isinstance(config, dict):
        return ["config must be a YAML mapping (key: value)"]
    errors = []
    try:
        build_devices(config, make_relay=lambda *a: None)
    except Exception as e:
        errors.append(str(e))
    try:
        from .camera import Camera, camera_configs
        for name, cfg in camera_configs(config).items():
            Camera(cfg, Path("."), None, name=name)
    except Exception as e:
        errors.append(f"camera: {e}")
    api = config.get("api") or {}
    if not isinstance(api.get("port", 8080), int):
        errors.append("api.port must be a number")
    return errors
