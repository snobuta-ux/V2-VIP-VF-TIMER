from datetime import datetime
from pathlib import Path

from src.camera import Camera, find_usb_camera, next_slot, photo_slots
from tests.test_cloud_log import FakeDB

T = datetime(2026, 11, 12, 9, 0, 0)


def make_node(sys_dir: Path, name: str, usb: bool, index: int = 0):
    target = sys_dir / "devices" / ("usb1/1-1.2/1-1.2:1.0" if usb else "platform/codec") / name
    target.mkdir(parents=True)
    node = sys_dir / name
    node.mkdir()
    (node / "device").symlink_to(target)
    (node / "index").write_text(f"{index}\n")


def test_finds_usb_webcam_and_skips_pi_codec_and_metadata_nodes(tmp_path):
    make_node(tmp_path, "video10", usb=False)
    make_node(tmp_path, "video2", usb=True, index=0)
    make_node(tmp_path, "video3", usb=True, index=1)  # metadata node
    assert find_usb_camera(tmp_path) == "/dev/video2"


def test_no_webcam(tmp_path):
    make_node(tmp_path, "video10", usb=False)
    assert find_usb_camera(tmp_path) is None
    assert find_usb_camera(tmp_path / "missing") is None


def test_schedule_slots():
    slots = photo_slots(T, "06:00", "21:00", 60)
    assert len(slots) == 16 and slots[0].hour == 6 and slots[-1].hour == 21
    assert next_slot(T, "06:00", "21:00", 60) == datetime(2026, 11, 12, 10, 0)
    assert next_slot(datetime(2026, 11, 12, 21, 30), "06:00", "21:00", 60) == datetime(2026, 11, 13, 6, 0)


class FakeFB:
    def __init__(self, db):
        self._db, self.device_id, self.events = db, "farm2", []

    def _server_ts(self):
        return "TS"

    def event(self, kind, **fields):
        self.events.append((kind, fields))


def fake_capture(device, out, *args):
    if device != "/dev/video2":
        raise RuntimeError("no such device")
    out.write_bytes(b"\xff\xd8jpeg")


def make_cam(tmp_path, db=None, devices=None, **cfg):
    devices = devices if devices is not None else ["/dev/video2"]
    fb = FakeFB(db) if db is not None else None
    cam = Camera({"upload_to": "firestore", **cfg}, tmp_path, fb, light_is_on=lambda: True,
                 clock=lambda: T, find=lambda: devices[0] if devices else None,
                 capture=fake_capture)
    return cam, fb


def test_snap_uploads_to_firestore_with_group_in_filename(tmp_path):
    db = FakeDB()
    cam, fb = make_cam(tmp_path, db)
    r = cam.snap()
    assert r["ok"] and r["uploaded"]
    assert r["filename"] == "timer_farm2_2026-11-12_09-00-00.jpg"
    doc = db.docs["grows/farm2/photos/timer_farm2_2026-11-12_09-00-00"]
    assert doc["jpeg"] == b"\xff\xd8jpeg" and doc["group"] == "timer" and doc["light_on"] is True
    assert (tmp_path / "data/photos/timer_farm2_2026-11-12_09-00-00.jpg").exists()
    assert not list((tmp_path / "data/photos/pending").iterdir())
    assert fb.events[0][0] == "photo"


def test_relatches_when_webcam_moves_to_another_port(tmp_path):
    devices = ["/dev/video0"]  # stale: the webcam is now on video2
    cam, _ = make_cam(tmp_path, FakeDB(), devices=devices)
    cam.device = "/dev/video0"
    devices[0] = "/dev/video2"
    assert cam.snap()["ok"]
    assert cam.device == "/dev/video2"


def test_no_webcam_does_not_raise(tmp_path):
    cam, _ = make_cam(tmp_path, FakeDB(), devices=[])
    r = cam.snap()
    assert not r["ok"] and "no USB webcam" in r["error"]


def test_offline_photo_is_kept_and_uploaded_later(tmp_path):
    db = FakeDB()
    db.fail = True
    cam, _ = make_cam(tmp_path, db)
    assert cam.snap()["uploaded"] is False
    pending = tmp_path / "data/photos/pending"
    assert len(list(pending.glob("*.jpg"))) == 1

    db.fail = False
    cam._upload_pending()
    assert not list(pending.glob("*.jpg"))
    assert "grows/farm2/photos/timer_farm2_2026-11-12_09-00-00" in db.docs


def test_custom_group(tmp_path):
    cam, _ = make_cam(tmp_path, FakeDB(), group="shelfA")
    assert cam.snap()["filename"].startswith("shelfA_farm2_")


class FakeBlob:
    def __init__(self, bucket, path):
        self.bucket, self.path, self.metadata = bucket, path, None

    def upload_from_filename(self, filename, content_type=None):
        self.bucket.uploaded[self.path] = Path(filename).read_bytes()


class FakeBucket:
    name = "proj.firebasestorage.app"

    def __init__(self):
        self.uploaded = {}

    def blob(self, path):
        return FakeBlob(self, path)


def test_uploads_to_firebase_storage_when_bucket_exists(tmp_path):
    db = FakeDB()
    cam, _ = make_cam(tmp_path, db, upload_to="auto")
    bucket = FakeBucket()
    cam._get_bucket = lambda: bucket
    assert cam.snap()["uploaded"]
    assert bucket.uploaded["grows/farm2/photos/timer_farm2_2026-11-12_09-00-00.jpg"] == b"\xff\xd8jpeg"
    doc = db.docs["grows/farm2/photos/timer_farm2_2026-11-12_09-00-00"]
    assert doc["stored_in"] == "storage" and "jpeg" not in doc
    assert doc["url"].startswith("https://firebasestorage.googleapis.com/v0/b/proj.firebasestorage.app/o/grows%2Ffarm2")
