import logging
import time
from datetime import datetime
from pathlib import Path

from src.cloud_log import CloudLogHandler, FirebaseLogger
from src.controller import Controller, Device
from src.relays import Relay
from src.schedules import Daily

TS = "SERVER_TS"


class FakeDoc:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def collection(self, name):
        return FakeCollection(self.db, f"{self.path}/{name}")

    def set(self, data, merge=False):
        if self.db.fail:
            raise ConnectionError("offline")
        if merge:
            self.db.docs.setdefault(self.path, {}).update(data)
        else:
            self.db.docs[self.path] = dict(data)


class FakeCollection:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def document(self, name):
        return FakeDoc(self.db, f"{self.path}/{name}")

    def add(self, data):
        if self.db.fail:
            raise ConnectionError("offline")
        self.db.added.setdefault(self.path, []).append(dict(data))


class FakeDB:
    def __init__(self):
        self.docs, self.added, self.fail = {}, {}, False

    def collection(self, name):
        return FakeCollection(self, name)


def wait_for(cond, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def make_fb(db, status=lambda: {"devices": {}}):
    return FirebaseLogger({"device_id": "farm2", "heartbeat_seconds": 10}, Path("."), status,
                          farm_info={"name": "test"}, client=db, server_timestamp=lambda: TS)


def test_writes_farm_info_status_and_events():
    db = FakeDB()
    fb = make_fb(db, status=lambda: {"devices": {"light": {"on": True}}})
    assert fb.start()
    fb.event("switch", device="pump", state="on")
    assert wait_for(lambda: len(db.added.get("grows/farm2/events", [])) >= 2)
    fb.stop()

    assert db.docs["grows/farm2"]["name"] == "test"
    status = db.docs["grows/farm2/live/status"]
    assert status["devices"]["light"]["on"] is True
    assert status["online"] is False          # last write on shutdown
    assert status["server_time"] == TS
    events = db.added["grows/farm2/events"]
    assert [e["type"] for e in events] == ["startup", "switch", "shutdown"]
    assert all(e["server_time"] == TS and "pi_time" in e for e in events)


def test_events_kept_while_offline_and_sent_later():
    db = FakeDB()
    db.fail = True
    fb = make_fb(db)
    import src.cloud_log as cl
    old, cl.RETRY_SECONDS = cl.RETRY_SECONDS, 0.1
    try:
        fb.start()
        fb.event("switch", device="pump", state="on")
        assert wait_for(lambda: fb.last_error is not None)
        assert "grows/farm2/events" not in db.added
        db.fail = False
        assert wait_for(lambda: len(db.added.get("grows/farm2/events", [])) == 2)
    finally:
        fb.stop()
        cl.RETRY_SECONDS = old
    assert [e["type"] for e in db.added["grows/farm2/events"]][:2] == ["startup", "switch"]


def test_missing_credentials_does_not_crash(tmp_path):
    fb = FirebaseLogger({"credentials_file": "nope.json"}, tmp_path, lambda: {})
    assert fb.start() is False
    fb.event("switch")   # still safe to call
    fb.stop()


def test_disabled():
    fb = FirebaseLogger({"enabled": False}, Path("."), lambda: {}, client=FakeDB())
    assert fb.start() is False


def test_controller_events_and_log_lines_reach_firebase():
    db = FakeDB()
    fb = make_fb(db)
    fb.start()
    clock_now = [datetime.fromisoformat("2026-11-01 06:00:00")]
    relay = Relay("light", 16, gpio=None)
    controller = Controller([Device("light", relay, Daily("06:00", "22:00"))],
                            clock=lambda: clock_now[0])
    controller.listeners.append(fb.controller_event)

    log = logging.getLogger("src.test_logger")
    handler = CloudLogHandler(fb)
    log.addHandler(handler)
    try:
        controller.tick()                       # light ON (schedule)
        controller.set_override("light", False, 60)
        log.warning("something odd")
        ok = wait_for(lambda: len(db.added.get("grows/farm2/events", [])) >= 5)
    finally:
        log.removeHandler(handler)
        fb.stop()
    assert ok
    events = db.added["grows/farm2/events"]
    types = [e["type"] for e in events]
    assert "switch" in types and "override" in types and "warning" in types
    sw = next(e for e in events if e["type"] == "switch")
    assert sw["device"] == "light" and sw["state"] == "on" and sw["source"] == "schedule"
    assert sw["pi_time"] == "2026-11-01T06:00:00"
    assert any("something odd" in line for line in db.docs["grows/farm2/live/logs"]["lines"])
