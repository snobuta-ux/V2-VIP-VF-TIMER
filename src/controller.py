"""The timer loop: once a second, set every relay to what its schedule says.

On top of the schedule there are two extra layers:

  manual override  - from the API: force a device on/off for a set time,
                     then it goes back to its schedule automatically.
  safety cut-off   - `max_run_seconds` in config.yaml. If a device stays on
                     longer than that (bad config, stuck override), it is
                     forced off and held off until its schedule says "off".
                     Always set this for the pump.
"""

import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Callable, Optional

from .relays import Relay
from .schedules import Schedule

logger = logging.getLogger(__name__)

TICK_SECONDS = 1.0

# How long a manual on/off lasts when the request doesn't say
DEFAULT_OVERRIDE_MINUTES = 60


class Device:
    def __init__(self, name: str, relay: Relay, schedule: Schedule,
                 max_run_seconds: Optional[float] = None):
        self.name = name
        self.relay = relay
        self.schedule = schedule
        self.max_run_seconds = max_run_seconds

        self.override_on: Optional[bool] = None
        self.override_until: Optional[datetime] = None
        self.on_since: Optional[datetime] = None
        self.locked_out = False
        self.total_on_seconds = 0.0


class Controller:
    def __init__(self, devices: list[Device], clock: Callable[[], datetime] = datetime.now):
        self.devices = {d.name: d for d in devices}
        self.clock = clock
        self.events: deque = deque(maxlen=200)
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ── main loop ────────────────────────────────────────────────────────────

    def run(self):
        logger.info("timer loop started")
        for d in self.devices.values():
            logger.info("  %-6s pin %-2d  %s%s", d.name, d.relay.pin, d.schedule.describe(),
                        f"  (safety cut-off {d.max_run_seconds:g}s)" if d.max_run_seconds else "")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("tick failed")
            self._stop.wait(TICK_SECONDS)

    def stop(self):
        self._stop.set()

    def tick(self):
        now = self.clock()
        with self._lock:
            for d in self.devices.values():
                self._update(d, now)

    def _update(self, d: Device, now: datetime):
        if d.override_until is not None and now >= d.override_until:
            logger.info("%s: manual override ended, back to schedule", d.name)
            d.override_on = d.override_until = None

        if d.override_on is not None:
            want, source = d.override_on, "manual"
        else:
            want, source = d.schedule.is_on(now), "schedule"

        # Safety cut-off stays engaged until the device would be off anyway
        if d.locked_out:
            if not want:
                d.locked_out = False
                logger.info("%s: safety cut-off cleared", d.name)
            want, source = False, "safety"

        if want and d.relay.is_on and d.max_run_seconds and d.on_since:
            if (now - d.on_since).total_seconds() >= d.max_run_seconds:
                logger.warning("%s: on longer than max_run_seconds (%gs) - forcing OFF",
                               d.name, d.max_run_seconds)
                d.locked_out = True
                d.override_on = d.override_until = None
                want, source = False, "safety"

        if want != d.relay.is_on:
            self._switch(d, want, source, now)

    def _switch(self, d: Device, on: bool, source: str, now: datetime):
        d.relay.set(on)
        if on:
            d.on_since = now
        else:
            if d.on_since:
                d.total_on_seconds += (now - d.on_since).total_seconds()
            d.on_since = None
        logger.info("%s -> %s (%s)", d.name, "ON" if on else "OFF", source)
        self.events.append({
            "time": now.isoformat(timespec="seconds"),
            "device": d.name,
            "state": "on" if on else "off",
            "source": source,
        })

    # ── manual control (used by the API) ─────────────────────────────────────

    def set_override(self, name: str, on: bool, seconds: Optional[float] = None) -> dict:
        d = self._get(name)
        if seconds is None:
            seconds = DEFAULT_OVERRIDE_MINUTES * 60
        if on and d.max_run_seconds:
            seconds = min(seconds, d.max_run_seconds)
        if seconds <= 0:
            raise ValueError("duration must be > 0")

        now = self.clock()
        with self._lock:
            d.override_on = on
            d.override_until = now + timedelta(seconds=seconds)
            d.locked_out = False
            logger.info("%s: manual %s for %gs", d.name, "ON" if on else "OFF", seconds)
            self._update(d, now)
        return self.device_status(d, now)

    def clear_override(self, name: str) -> dict:
        d = self._get(name)
        now = self.clock()
        with self._lock:
            d.override_on = d.override_until = None
            logger.info("%s: back to schedule", d.name)
            self._update(d, now)
        return self.device_status(d, now)

    def _get(self, name: str) -> Device:
        if name not in self.devices:
            raise KeyError(name)
        return self.devices[name]

    # ── status ───────────────────────────────────────────────────────────────

    def device_status(self, d: Device, now: datetime) -> dict:
        until_change = d.schedule.seconds_until_change(now)
        on_seconds = d.total_on_seconds
        if d.on_since:
            on_seconds += (now - d.on_since).total_seconds()
        return {
            "on": d.relay.is_on,
            "pin": d.relay.pin,
            "simulated": d.relay.simulated,
            "schedule": d.schedule.describe(),
            "schedule_says": "on" if d.schedule.is_on(now) else "off",
            "next_schedule_change": (
                (now + timedelta(seconds=until_change)).isoformat(timespec="seconds")
                if until_change is not None else None
            ),
            "manual_override": (
                {"state": "on" if d.override_on else "off",
                 "until": d.override_until.isoformat(timespec="seconds")}
                if d.override_on is not None else None
            ),
            "safety_cutoff_active": d.locked_out,
            "max_run_seconds": d.max_run_seconds,
            "on_seconds_since_start": round(on_seconds, 1),
        }

    def status(self) -> dict:
        now = self.clock()
        with self._lock:
            return {
                "time": now.isoformat(timespec="seconds"),
                "devices": {name: self.device_status(d, now) for name, d in self.devices.items()},
                "recent_events": list(self.events)[-20:],
            }
