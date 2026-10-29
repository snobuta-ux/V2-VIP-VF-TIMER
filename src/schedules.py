"""Timer schedules for the relays.

Every schedule answers one question: "should this device be on at time X?"
The answer depends only on the clock, never on what happened before, so a
reboot or crash can't make the system water twice or skip a cycle — when the
program starts back up it just looks at the clock and picks up where the
schedule says it should be.

Schedule types (set in config.yaml):

  daily      on between two clock times every day          (grow light)
             on_time: "06:00", off_time: "22:00"

  interval   run for N seconds every M minutes             (pump, fan)
             every_minutes: 360, run_seconds: 5, start_at: "06:00"

  times      run for N seconds at fixed clock times        (pump)
             at: ["07:00", "19:00"], run_seconds: 5

  always_on  always on
  off        always off
"""

from datetime import datetime
from typing import Optional

DAY = 24 * 60 * 60

# Interval schedules count from this date so the slots line up the same way
# every day (e.g. every 6 h from 06:00 -> 06:00, 12:00, 18:00, 00:00).
_REFERENCE_DATE = datetime(2000, 1, 1)


def parse_hhmm(value: str) -> int:
    """'06:30' -> seconds since midnight."""
    try:
        hours, minutes = str(value).strip().split(":")
        hours, minutes = int(hours), int(minutes)
    except ValueError:
        raise ValueError(f"time must look like HH:MM, got {value!r}")
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"time out of range: {value!r}")
    return hours * 3600 + minutes * 60


def seconds_into_day(now: datetime) -> float:
    return now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6


class Schedule:
    def is_on(self, now: datetime) -> bool:
        raise NotImplementedError

    def seconds_until_change(self, now: datetime) -> Optional[float]:
        """Seconds until the on/off state next flips, or None if it never does."""
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


class AlwaysOn(Schedule):
    def is_on(self, now):
        return True

    def seconds_until_change(self, now):
        return None

    def describe(self):
        return "always on"


class AlwaysOff(Schedule):
    def is_on(self, now):
        return False

    def seconds_until_change(self, now):
        return None

    def describe(self):
        return "off"


class Daily(Schedule):
    """On from `on_time` to `off_time` every day. Works across midnight (e.g. 20:00 -> 04:00)."""

    def __init__(self, on_time: str, off_time: str):
        self.on_text, self.off_text = on_time, off_time
        self.on_s = parse_hhmm(on_time)
        self.off_s = parse_hhmm(off_time)
        if self.on_s == self.off_s:
            raise ValueError("daily schedule: on_time and off_time can't be the same "
                             "(use type: always_on or type: off instead)")

    def is_on(self, now):
        t = seconds_into_day(now)
        if self.on_s < self.off_s:
            return self.on_s <= t < self.off_s
        return t >= self.on_s or t < self.off_s  # window crosses midnight

    def seconds_until_change(self, now):
        target = self.off_s if self.is_on(now) else self.on_s
        return (target - seconds_into_day(now)) % DAY

    def describe(self):
        return f"daily {self.on_text}-{self.off_text}"


class Interval(Schedule):
    """Run for `run_seconds` every `every_minutes`, starting from `start_at`."""

    def __init__(self, every_minutes: float, run_seconds: float, start_at: str = "00:00"):
        self.every_minutes = every_minutes
        self.run_seconds = float(run_seconds)
        self.start_at = start_at
        self.every_s = float(every_minutes) * 60
        if self.every_s <= 0 or self.run_seconds <= 0:
            raise ValueError("interval schedule: every_minutes and run_seconds must be > 0")
        if self.run_seconds >= self.every_s:
            raise ValueError("interval schedule: run_seconds must be shorter than every_minutes")
        self.start_s = parse_hhmm(start_at)

    def _offset(self, now):
        elapsed = (now - _REFERENCE_DATE).total_seconds() - self.start_s
        return elapsed % self.every_s  # seconds since the current slot started

    def is_on(self, now):
        return self._offset(now) < self.run_seconds

    def seconds_until_change(self, now):
        offset = self._offset(now)
        if offset < self.run_seconds:
            return self.run_seconds - offset
        return self.every_s - offset

    def describe(self):
        return (f"{self.run_seconds:g}s every {self.every_minutes:g} min "
                f"(from {self.start_at})")


class Times(Schedule):
    """Run for `run_seconds` starting at each clock time in `at`."""

    def __init__(self, at: list, run_seconds: float):
        if not at:
            raise ValueError("times schedule: 'at' needs at least one time")
        self.at_text = list(at)
        self.starts = sorted(parse_hhmm(t) for t in at)
        self.run_seconds = float(run_seconds)
        if self.run_seconds <= 0:
            raise ValueError("times schedule: run_seconds must be > 0")
        gaps = [(b - a) % DAY or DAY for a, b in zip(self.starts, self.starts[1:] + self.starts[:1])]
        if self.run_seconds >= min(gaps):
            raise ValueError("times schedule: run_seconds is longer than the gap between times")

    def is_on(self, now):
        t = seconds_into_day(now)
        return any((t - s) % DAY < self.run_seconds for s in self.starts)

    def seconds_until_change(self, now):
        t = seconds_into_day(now)
        for s in self.starts:
            offset = (t - s) % DAY
            if offset < self.run_seconds:
                return self.run_seconds - offset
        return min((s - t) % DAY for s in self.starts)

    def describe(self):
        return f"{self.run_seconds:g}s at {', '.join(self.at_text)}"


def build_schedule(cfg: dict) -> Schedule:
    cfg = dict(cfg or {})
    kind = cfg.pop("type", "off")
    builders = {
        "daily": Daily,
        "interval": Interval,
        "times": Times,
        "always_on": AlwaysOn,
        "off": AlwaysOff,
    }
    if kind not in builders:
        raise ValueError(f"unknown schedule type {kind!r} (use one of: {', '.join(builders)})")
    try:
        return builders[kind](**cfg)
    except TypeError as e:
        raise ValueError(f"{kind} schedule has a wrong or missing setting: {e}")
