from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from src.api_server import create_app
from src.controller import Controller, Device
from src.main import build_devices
from src.relays import Relay
from src.schedules import AlwaysOn, Daily, Interval


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += timedelta(seconds=seconds)


class FakeGPIO:
    BOARD, OUT, HIGH, LOW = "BOARD", "OUT", 1, 0

    def __init__(self):
        self.pins = {}

    def setup(self, pin, mode):
        pass

    def output(self, pin, level):
        self.pins[pin] = level


def make(schedule, max_run=None, start="2026-11-01 05:59:58"):
    clock = FakeClock(datetime.fromisoformat(start))
    relay = Relay("dev", 13, gpio=None)
    controller = Controller([Device("dev", relay, schedule, max_run)], clock=clock)
    return controller, relay, clock


def test_relay_active_low_levels():
    gpio = FakeGPIO()
    relay = Relay("light", 13, active_low=True, gpio=gpio)
    assert gpio.pins[13] == FakeGPIO.HIGH  # starts OFF
    relay.set(True)
    assert gpio.pins[13] == FakeGPIO.LOW
    relay.set(False)
    assert gpio.pins[13] == FakeGPIO.HIGH


def test_follows_schedule():
    controller, relay, clock = make(Daily("06:00", "22:00"))
    controller.tick()
    assert not relay.is_on
    clock.advance(2)
    controller.tick()
    assert relay.is_on
    assert controller.events[-1]["source"] == "schedule"


def test_manual_override_expires_back_to_schedule():
    controller, relay, clock = make(Daily("06:00", "22:00"), start="2026-11-01 12:00:00")
    controller.tick()
    assert relay.is_on
    controller.set_override("dev", False, seconds=60)
    assert not relay.is_on
    clock.advance(59)
    controller.tick()
    assert not relay.is_on
    clock.advance(1)
    controller.tick()
    assert relay.is_on


def test_manual_on_is_capped_by_max_run_seconds():
    controller, relay, clock = make(Interval(360, 5), max_run=60, start="2026-11-01 03:00:00")
    status = controller.set_override("dev", True, seconds=3600)
    assert relay.is_on
    assert status["manual_override"]["until"] == "2026-11-01T03:01:00"
    clock.advance(60)
    controller.tick()
    assert not relay.is_on


def test_safety_cutoff_holds_until_schedule_turns_off():
    controller, relay, clock = make(AlwaysOn(), max_run=60)
    controller.tick()
    assert relay.is_on
    clock.advance(60)
    controller.tick()
    assert not relay.is_on
    assert controller.events[-1]["source"] == "safety"
    clock.advance(600)
    controller.tick()
    assert not relay.is_on  # stays off - schedule still says "on"


def test_unknown_device():
    controller, _, _ = make(AlwaysOn())
    with pytest.raises(KeyError):
        controller.set_override("heater", True)


def test_shipped_config_builds():
    config = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
    devices = build_devices(config)
    assert {d.name for d in devices} == {"light", "pump", "fan"}
    assert {d.relay.pin for d in devices} == {13, 11, 15}


def test_config_rejects_pump_longer_than_cutoff():
    config = {"devices": {"pump": {"pin": 11, "max_run_seconds": 60, "schedule": {
        "type": "interval", "every_minutes": 360, "run_seconds": 120}}}}
    with pytest.raises(ValueError, match="max_run_seconds"):
        build_devices(config)


def test_config_rejects_duplicate_pins():
    config = {"devices": {"a": {"pin": 11, "schedule": {"type": "off"}},
                          "b": {"pin": 11, "schedule": {"type": "off"}}}}
    with pytest.raises(ValueError, match="already used"):
        build_devices(config)


def test_api():
    controller, relay, _ = make(Daily("06:00", "22:00"), start="2026-11-01 12:00:00")
    controller.tick()
    client = TestClient(create_app(controller, api_key="secret"))

    assert client.get("/api/status").json()["devices"]["dev"]["on"] is True
    assert client.post("/api/dev/off", json={"minutes": 5}).status_code == 401

    headers = {"X-API-Key": "secret"}
    assert client.post("/api/dev/off", json={"minutes": 5}, headers=headers).status_code == 200
    assert not relay.is_on
    assert client.post("/api/dev/auto", headers=headers).status_code == 200
    assert relay.is_on
    assert client.post("/api/heater/on", headers=headers).status_code == 404
