import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from src.camera import assign_cameras, build_cameras, camera_configs, find_usb_cameras
from src.config import load_config, validate_config
from src.controller import Controller, Device
from src.relays import Relay
from src.remote import RemoteControl
from src.schedules import Daily, Times
from tests.test_camera import make_node
from tests.test_controller import FakeClock, FakeGPIO

REPO = Path(__file__).resolve().parent.parent


# ── the real config.yaml ─────────────────────────────────────────────

def test_shipped_config_is_valid_and_pump_runs_10_min_three_times_a_day():
    cfg = load_config(REPO, REPO / "config.yaml")
    assert validate_config(cfg) == []
    pump = cfg["devices"]["pump"]
    assert pump["schedule"] == {"type": "times", "at": ["07:00", "12:00", "19:00"], "run_seconds": 600}
    assert pump["max_run_seconds"] >= 600
    assert set(camera_configs(cfg)) == {"plant", "dashboard"}


def test_pump_schedule_is_on_for_exactly_10_minutes():
    s = Times(["07:00", "12:00", "19:00"], 600)
    day = datetime(2026, 11, 19)
    on_minutes = sum(s.is_on(day.replace(hour=h, minute=m)) for h in range(24) for m in range(60))
    assert on_minutes == 30
    assert s.is_on(day.replace(hour=12, minute=9, second=59))
    assert not s.is_on(day.replace(hour=12, minute=10))


def test_local_config_overrides_base(tmp_path):
    (tmp_path / "config.yaml").write_text("a: 1\ncameras:\n  plant: {device: auto}\n")
    (tmp_path / "config.local.yaml").write_text("cameras:\n  plant: {device: 1-1.3}\n")
    assert load_config(tmp_path) == {"a": 1, "cameras": {"plant": {"device": "1-1.3"}}}


def test_validate_catches_bad_config_without_touching_gpio():
    errors = validate_config({"devices": {"pump": {"pin": 11, "max_run_seconds": 60,
                                                   "schedule": {"type": "times", "at": ["07:00"],
                                                                "run_seconds": 600}}}})
    assert errors and "max_run_seconds" in errors[0]
    assert validate_config("nope")
    assert validate_config({"devices": {"x": {"pin": 1, "schedule": {"type": "off"}}},
                            "cameras": {"kitchen": {}}})


# ── two webcams ──────────────────────────────────────────────────────

def test_finds_both_webcams_in_usb_port_order(tmp_path):
    def node(name, port):
        target = tmp_path / "devices" / f"usb1/1-1/{port}/{port}:1.0" / name
        target.mkdir(parents=True)
        (tmp_path / name).mkdir()
        (tmp_path / name / "device").symlink_to(target)
        (tmp_path / name / "index").write_text("0\n")
    node("video2", "1-1.3")
    node("video0", "1-1.2")
    make_node(tmp_path, "video10", usb=False)
    assert [(c["dev"], c["port"]) for c in find_usb_cameras(tmp_path)] == \
        [("/dev/video0", "1-1.2"), ("/dev/video2", "1-1.3")]


FOUND = [{"dev": "/dev/video0", "port": "1-1.2"}, {"dev": "/dev/video2", "port": "1-1.3"}]


def test_assign_auto_and_pinned():
    assert assign_cameras(FOUND, {"plant": "auto", "dashboard": "auto"}) == \
        {"plant": "/dev/video0", "dashboard": "/dev/video2"}
    assert assign_cameras(FOUND, {"plant": "1-1.3", "dashboard": "auto"}) == \
        {"plant": "/dev/video2", "dashboard": "/dev/video0"}
    assert assign_cameras(FOUND[:1], {"plant": "auto", "dashboard": "auto"}) == \
        {"plant": "/dev/video0", "dashboard": None}
    assert assign_cameras(FOUND, {"plant": "1-9", "dashboard": "auto"})["plant"] is None


def test_dashboard_camera_filename_and_doc(tmp_path):
    from tests.test_camera import FakeFB, T, fake_capture
    from tests.test_cloud_log import FakeDB
    db = FakeDB()
    fb = FakeFB(db)
    cams = build_cameras({"camera": {"upload_to": "firestore"},
                          "cameras": {"plant": {}, "dashboard": {"only_when_light_on": False}}},
                         tmp_path, fb, light_is_on=lambda: False,
                         find_all=lambda: [{"dev": "/dev/video9", "port": "1-1"},
                                           {"dev": "/dev/video2", "port": "1-2"}],
                         clock=lambda: T, capture=fake_capture)
    r = cams["dashboard"].snap()
    assert r["ok"] and r["filename"] == "timer_farm2_dashboard_2026-11-12_09-00-00.jpg"
    doc = db.docs["grows/farm2/photos/timer_farm2_dashboard_2026-11-12_09-00-00"]
    assert doc["camera"] == "dashboard"
    assert (tmp_path / "data/photos/dashboard/timer_farm2_dashboard_2026-11-12_09-00-00.jpg").exists()
    assert fb.events[-1][1]["camera"] == "dashboard"
    # the plant camera got /dev/video9, which fake_capture can't use
    assert not cams["plant"].snap()["ok"]


def test_old_single_camera_config_still_works():
    assert camera_configs({"camera": {"group": "g"}}) == {"plant": {"group": "g"}}


# ── remote commands ──────────────────────────────────────────────────

class FakeFB:
    device_id = "farm2"

    def __init__(self):
        self._db, self.events, self.pokes = None, [], 0

    def event(self, kind, **fields):
        self.events.append((kind, fields))

    def write_status_soon(self):
        self.pokes += 1


def make_remote(tmp_path, cameras=None):
    gpio = FakeGPIO()
    clock = FakeClock(datetime(2026, 11, 19, 8, 0, 0))
    pump = Device("pump", Relay("pump", 11, gpio=gpio), Times(["07:00", "12:00", "19:00"], 600),
                  max_run_seconds=660)
    light = Device("light", Relay("light", 16, gpio=gpio), Daily("06:00", "22:00"))
    ctl = Controller([pump, light], clock=clock)
    ctl.tick()
    restarts = []
    rc = RemoteControl(ctl, FakeFB(), cameras or {}, {}, tmp_path,
                       restart_fn=restarts.append, find_cameras=lambda: FOUND)
    calls = []
    rc.set_command = lambda cmd_id, status, **kw: calls.append((status, kw))
    rc.request_restart = lambda code=42: restarts.append(code)
    return rc, ctl, clock, calls, restarts


def test_pump_on_is_capped_and_returns_to_schedule(tmp_path):
    rc, ctl, clock, calls, _ = make_remote(tmp_path)
    rc.run_command("c1", {"type": "device_on", "args": {"device": "pump", "minutes": 30},
                          "created_epoch": time.time()})
    assert calls[-1][0] == "done" and "safety limit" in calls[-1][1]["result"]["message"]
    assert ctl.devices["pump"].relay.is_on
    clock.advance(661)
    ctl.tick()
    assert not ctl.devices["pump"].relay.is_on
    override = next(e for e in ctl.events if e["type"] == "override")
    assert override["source"] == "website" and override["seconds"] == 660


def test_light_off_then_auto(tmp_path):
    rc, ctl, clock, calls, _ = make_remote(tmp_path)
    assert ctl.devices["light"].relay.is_on
    rc.run_command("c1", {"type": "device_off", "args": {"device": "light", "minutes": 10}})
    assert not ctl.devices["light"].relay.is_on
    rc.run_command("c2", {"type": "device_auto", "args": {"device": "light"}})
    assert ctl.devices["light"].relay.is_on and calls[-1][0] == "done"
    assert rc.fb.pokes == 2      # status pushed after every command


def test_unknown_expired_and_bad_device(tmp_path):
    rc, _, _, calls, _ = make_remote(tmp_path)
    rc.run_command("c1", {"type": "rm_rf"})
    assert calls[-1][0] == "error"
    rc.run_command("c2", {"type": "ping", "created_epoch": time.time() - 3600})
    assert calls[-1][0] == "expired"
    rc.run_command("c3", {"type": "device_on", "args": {"device": "toaster"}})
    assert calls[-1][0] == "error" and "unknown device" in str(calls[-1][1]["error"])


def git_repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.py").write_text("X = 1\n")
    (tmp_path / "config.yaml").write_text((REPO / "config.yaml").read_text())
    (tmp_path / "supervisor.py").write_text("# safety net\n")
    for cmd in (["init", "-q"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t",
                                              "commit", "-qm", "init"]):
        subprocess.run(["git", *cmd], cwd=tmp_path, check=True, capture_output=True)


def test_code_editor_lists_reads_and_saves_with_backup_and_rollback_info(tmp_path):
    git_repo(tmp_path)
    rc, _, _, calls, restarts = make_remote(tmp_path)
    files = [f["path"] for f in rc.cmd_list_files({}, "c")["files"]]
    assert "src/thing.py" in files and "config.yaml" in files and "config.local.yaml" in files
    assert "supervisor.py" not in files
    assert rc.cmd_get_file({"path": "src/thing.py"}, "c")["text"] == "X = 1\n"

    assert rc.cmd_set_file({"path": "src/thing.py", "text": "X = 2\n"}, "c9") is None
    assert (tmp_path / "src/thing.py").read_text() == "X = 2\n"
    assert restarts == [42]
    state = json.loads((tmp_path / "data/update_state.json").read_text())
    assert state["state"] == "restarting" and state["command_id"] == "c9"
    backup = state["rollback"]["files"][0]["backup"]
    assert (tmp_path / backup).read_text() == "X = 1\n"
    assert rc.cmd_list_files({}, "c")["files"][-1]  # still works
    assert "src/thing.py" in rc._version(fresh=True)["local_changes"]


def test_code_editor_refuses_bad_python_bad_config_and_other_paths(tmp_path):
    git_repo(tmp_path)
    rc, _, _, _, restarts = make_remote(tmp_path)
    with pytest.raises(ValueError, match="syntax error"):
        rc.cmd_set_file({"path": "src/thing.py", "text": "def (:\n"}, "c")
    with pytest.raises(ValueError, match="wouldn't start"):
        rc.cmd_set_config({"file": "base", "text": "devices: {}\n"}, "c")
    with pytest.raises(ValueError, match="YAML"):
        rc.cmd_set_config({"file": "local", "text": "a: [\n"}, "c")
    for bad in ("supervisor.py", "../etc/passwd", "firebase-credentials.json", ".env"):
        with pytest.raises(ValueError, match="can't be edited"):
            rc.cmd_get_file({"path": bad}, "c")
    assert (tmp_path / "src/thing.py").read_text() == "X = 1\n"
    assert restarts == []


def test_swap_cameras_writes_local_config(tmp_path):
    git_repo(tmp_path)

    class Cam:
        def __init__(self, dev):
            self.device = dev
    rc, _, _, _, restarts = make_remote(tmp_path, cameras={"plant": Cam("/dev/video0"),
                                                           "dashboard": Cam("/dev/video2")})
    rc.cmd_swap_cameras({}, "c")
    local = yaml.safe_load((tmp_path / "config.local.yaml").read_text())
    assert local["cameras"]["plant"]["device"] == "1-1.3"
    assert local["cameras"]["dashboard"]["device"] == "1-1.2"
    assert restarts == [42]


def test_startup_reports_success_and_rollback(tmp_path):
    rc, _, _, calls, _ = make_remote(tmp_path)
    rc._running = True
    rc.write_update_state({"state": "restarting", "kind": "file", "command_id": "c1", "note": "Saved x"})
    rc._verify_startup(wait=0)
    assert calls[-1][0] == "done" and "stayed up" in calls[-1][1]["result"]["message"]

    rc.write_update_state({"state": "rolled_back", "command_id": "c2", "rollback_note": "restored x",
                           "log_tail": ["Traceback", "NameError"]})
    rc._verify_startup(wait=0)
    assert calls[-1][0] == "error" and "NameError" in calls[-1][1]["error"]
    assert rc.read_update_state()["state"] == "reported"


# ── supervisor.py ────────────────────────────────────────────────────

def test_supervisor_rolls_back_files(tmp_path, monkeypatch):
    sys.path.insert(0, str(REPO))
    import supervisor
    monkeypatch.setattr(supervisor, "BASE", tmp_path)
    (tmp_path / "a.py").write_text("broken")
    (tmp_path / "bak").write_text("good")
    (tmp_path / "new.py").write_text("new")
    note = supervisor.rollback({"rollback": {"type": "files", "files": [
        {"path": "a.py", "backup": "bak"}, {"path": "new.py", "backup": None}]}})
    assert (tmp_path / "a.py").read_text() == "good" and not (tmp_path / "new.py").exists()
    assert "restored a.py" in note
