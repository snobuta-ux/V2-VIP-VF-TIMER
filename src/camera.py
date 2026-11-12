"""USB webcam: take routine photos and upload them to Firebase.

- Finds the USB webcam on any USB port and keeps using it. If it's unplugged or
  moved to another port, it looks again before the next photo.
- Takes a photo every `every_minutes` between `start_at` and `end_at`
  (optionally only while the light is on, so there are no black night photos).
- Filename: <group>_<device_id>_<YYYY-MM-DD_HH-MM-SS>.jpg, e.g.
  timer_farm2_2026-11-12_09-00-00.jpg
- Every photo is saved in data/photos/ first, then uploaded:
    Firebase Storage   grows/<device_id>/photos/<filename>
    Firestore          grows/<device_id>/photos/<filename without .jpg>  (info + link)
  If the project has no Storage bucket, the JPEG itself goes in the Firestore
  doc (field `jpeg`) instead.
- Photos that couldn't be uploaded (offline) stay in data/photos/pending/ and
  are retried, even after a restart.

Like Firebase logging, the camera must never stop the timer: every failure is
logged as a warning and the timer keeps running.
"""

import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

V4L_SYS = Path("/sys/class/video4linux")
FIRESTORE_MAX_JPEG = 900_000  # a Firestore doc can be at most 1 MiB
RETRY_SECONDS = 300


def find_usb_camera(sys_dir: Path = V4L_SYS) -> Optional[str]:
    """Return /dev/videoN of the first USB webcam, or None.

    A USB webcam shows up as two nodes (videoN for pictures, videoN+1 for
    metadata) - only the one with index 0 can take pictures. The Pi's own
    video nodes (codec, ISP) aren't on USB, so they're skipped.
    """
    if not sys_dir.exists():
        return None
    nodes = sorted(sys_dir.glob("video*"), key=lambda p: int(p.name[5:] or 0))
    for node in nodes:
        try:
            if "/usb" not in str((node / "device").resolve()):
                continue
            index = (node / "index").read_text().strip() if (node / "index").exists() else "0"
            if index != "0":
                continue
        except OSError:
            continue
        return f"/dev/{node.name}"
    return None


def fswebcam_capture(device: str, out: Path, resolution: str, skip_frames: int,
                     quality: int) -> None:
    """Take one JPEG with fswebcam. Raises RuntimeError on failure."""
    if not shutil.which("fswebcam"):
        raise RuntimeError("fswebcam is not installed (run: bash install.sh)")
    cmd = ["fswebcam", "-q", "-d", device, "-r", resolution, "--no-banner",
           "-S", str(skip_frames), "--jpeg", str(quality), str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if not out.exists() or out.stat().st_size == 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise RuntimeError(f"fswebcam on {device} made no picture: {msg[-1] if msg else 'no output'}")


def photo_slots(day: datetime, start_at: str, end_at: str, every_minutes: float) -> list[datetime]:
    """All photo times on `day` from start_at to end_at (inclusive)."""
    sh, sm = map(int, start_at.split(":"))
    eh, em = map(int, end_at.split(":"))
    t = day.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = day.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end < t:
        end += timedelta(days=1)  # window crosses midnight
    slots = []
    while t <= end:
        slots.append(t)
        t += timedelta(minutes=every_minutes)
    return slots


def next_slot(now: datetime, start_at: str, end_at: str, every_minutes: float) -> datetime:
    for day_offset in (-1, 0, 1):
        day = now + timedelta(days=day_offset)
        for t in photo_slots(day, start_at, end_at, every_minutes):
            if t > now:
                return t
    raise ValueError("camera schedule has no photo times")


class Camera:
    def __init__(self, config: dict, base_dir: Path, fb=None,
                 light_is_on: Optional[Callable[[], bool]] = None,
                 clock: Callable[[], datetime] = datetime.now,
                 find: Callable[[], Optional[str]] = find_usb_camera,
                 capture: Callable = fswebcam_capture):
        cfg = config or {}
        self.enabled = cfg.get("enabled", True)
        self.group = str(cfg.get("group", "timer"))
        self.every_minutes = float(cfg.get("every_minutes", 60))
        self.start_at = str(cfg.get("start_at", "06:00"))
        self.end_at = str(cfg.get("end_at", "21:00"))
        self.only_when_light_on = cfg.get("only_when_light_on", True)
        self.resolution = str(cfg.get("resolution", "1280x720"))
        self.skip_frames = int(cfg.get("skip_frames", 20))
        self.quality = int(cfg.get("jpeg_quality", 85))
        self.upload_to = cfg.get("upload_to", "auto")  # auto | storage | firestore
        self.storage_bucket = cfg.get("storage_bucket") or None
        self.keep_days = float(cfg.get("keep_days", 30))
        if self.every_minutes <= 0:
            raise ValueError("camera: every_minutes must be > 0")
        next_slot(datetime(2026, 1, 1), self.start_at, self.end_at, self.every_minutes)

        self.fb = fb
        self.device_id = fb.device_id if fb is not None else "farm2"
        self.light_is_on = light_is_on
        self.clock = clock
        self._find = find
        self._capture = capture

        self.photo_dir = base_dir / "data" / "photos"
        self.pending_dir = self.photo_dir / "pending"
        self.device: Optional[str] = None  # the webcam we latched on to
        self._bucket = None
        self._bucket_checked = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_photo: Optional[dict] = None
        self.next_photo: Optional[str] = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> bool:
        if not self.enabled:
            logger.info("camera: disabled in config.yaml")
            return False
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.device = self._find()
        if self.device:
            logger.info("camera: using USB webcam %s", self.device)
        else:
            logger.warning("camera: no USB webcam found yet - will keep looking before each photo")
        logger.info("camera: photo every %g min from %s to %s%s", self.every_minutes,
                    self.start_at, self.end_at,
                    " (only while the light is on)" if self.only_when_light_on else "")
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def status(self) -> dict:
        return {"enabled": self.enabled, "webcam": self.device, "group": self.group,
                "every_minutes": self.every_minutes, "window": f"{self.start_at}-{self.end_at}",
                "next_photo": self.next_photo, "last_photo": self.last_photo,
                "pending_uploads": len(list(self.pending_dir.glob("*.jpg")))
                if self.pending_dir.exists() else 0}

    def _run(self):
        self._upload_pending()
        while not self._stop.is_set():
            now = self.clock()
            due = next_slot(now, self.start_at, self.end_at, self.every_minutes)
            self.next_photo = due.isoformat(timespec="seconds")
            # Wake up at least every RETRY_SECONDS to retry failed uploads.
            while not self._stop.is_set() and self.clock() < due:
                wait = min(RETRY_SECONDS, (due - self.clock()).total_seconds())
                if self._stop.wait(max(0.5, wait)):
                    return
                self._upload_pending()
            if self._stop.is_set():
                return
            if self.only_when_light_on and self.light_is_on and not self.light_is_on():
                logger.info("camera: light is off, skipping the %s photo", due.strftime("%H:%M"))
                continue
            self.snap("schedule")
            self._prune()

    # ── taking a photo ───────────────────────────────────────────────────────

    def filename(self, when: datetime) -> str:
        return f"{self.group}_{self.device_id}_{when.strftime('%Y-%m-%d_%H-%M-%S')}.jpg"

    def snap(self, reason: str = "manual") -> dict:
        """Take a photo now and upload it. Never raises; returns what happened."""
        with self._lock:
            when = self.clock()
            name = self.filename(when)
            path = self.pending_dir / name
            self.pending_dir.mkdir(parents=True, exist_ok=True)
            try:
                self._take(path)
            except Exception as e:
                logger.warning("camera: photo failed (%s)", e)
                return {"ok": False, "error": str(e)}
            info = {"filename": name, "pi_time": when.astimezone().isoformat(timespec="seconds"),
                    "reason": reason, "webcam": self.device, "bytes": path.stat().st_size}
            if self.light_is_on:
                try:
                    info["light_on"] = bool(self.light_is_on())
                except Exception:
                    pass
            path.with_suffix(".json").write_text(json.dumps(info))
            logger.info("camera: took %s (%d KB)", name, info["bytes"] // 1024)
            uploaded = self._upload(path, info)
            self.last_photo = {**info, "uploaded": uploaded}
            return {"ok": True, **self.last_photo}

    def _take(self, path: Path):
        # Latch on to the webcam we found; if it stops working (unplugged,
        # moved to the other port), search again once and retry.
        for attempt in (1, 2):
            if not self.device:
                self.device = self._find()
                if not self.device:
                    raise RuntimeError("no USB webcam found - is it plugged in?")
                logger.info("camera: using USB webcam %s", self.device)
            try:
                self._capture(self.device, path, self.resolution, self.skip_frames, self.quality)
                return
            except Exception:
                path.unlink(missing_ok=True)
                old, self.device = self.device, None
                if attempt == 2:
                    raise
                logger.info("camera: %s didn't work, looking for the webcam again", old)

    # ── uploading ────────────────────────────────────────────────────────────

    def _upload_pending(self):
        if not self.pending_dir.exists() or self.fb is None or getattr(self.fb, "_db", None) is None:
            return
        for path in sorted(self.pending_dir.glob("*.jpg")):
            if self._stop.is_set():
                return
            with self._lock:
                if not path.exists():
                    continue
                meta = path.with_suffix(".json")
                info = json.loads(meta.read_text()) if meta.exists() else {"filename": path.name}
                if not self._upload(path, info):
                    return  # still offline; try again later

    def _upload(self, path: Path, info: dict) -> bool:
        """Upload one photo. On success move it out of pending/. Returns success."""
        fb = self.fb
        db = getattr(fb, "_db", None) if fb is not None else None
        if db is None:
            return False
        doc_id = path.stem
        data = {**info, "group": self.group, "device_id": self.device_id,
                "server_time": fb._server_ts()}
        try:
            bucket = self._get_bucket()
            if bucket is not None:
                storage_path = f"grows/{self.device_id}/photos/{path.name}"
                blob = bucket.blob(storage_path)
                token = str(uuid.uuid4())
                blob.metadata = {"firebaseStorageDownloadTokens": token}
                blob.upload_from_filename(str(path), content_type="image/jpeg")
                data.update({
                    "stored_in": "storage", "bucket": bucket.name, "storage_path": storage_path,
                    "url": (f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/"
                            f"{quote(storage_path, safe='')}?alt=media&token={token}"),
                })
            else:
                jpeg = path.read_bytes()
                if len(jpeg) > FIRESTORE_MAX_JPEG:
                    raise RuntimeError(f"{path.name} is {len(jpeg)//1024} KB, too big for Firestore "
                                       "(lower camera.resolution or jpeg_quality)")
                data.update({"stored_in": "firestore", "jpeg": jpeg})
            db.collection("grows").document(self.device_id) \
              .collection("photos").document(doc_id).set(data)
        except Exception as e:
            logger.warning("camera: upload of %s failed (%s: %s) - kept, will retry",
                           path.name, type(e).__name__, e)
            return False

        path.replace(self.photo_dir / path.name)
        path.with_suffix(".json").unlink(missing_ok=True)
        where = data.get("storage_path") or f"grows/{self.device_id}/photos/{doc_id} (Firestore)"
        logger.info("camera: uploaded %s -> %s", path.name, where)
        fb.event("photo", filename=path.name, stored_in=data["stored_in"],
                 **{k: data[k] for k in ("storage_path", "url") if k in data})
        return True

    def _get_bucket(self):
        """The Firebase Storage bucket, or None to store photos in Firestore."""
        if self.upload_to == "firestore":
            return None
        if self._bucket_checked:
            return self._bucket
        from firebase_admin import storage
        app = self.fb.firebase_app()
        names = [self.storage_bucket] if self.storage_bucket else []
        if not names:
            project = app.project_id or _project_id(self.fb.credentials_path)
            names = [f"{project}.firebasestorage.app", f"{project}.appspot.com"]
        for name in names:
            bucket = storage.bucket(name, app=app)
            try:
                found = bucket.exists()
            except Exception as e:
                # No access to that bucket counts as "not there"; anything else
                # (offline) is raised so we check again on the next upload.
                if type(e).__name__ not in ("Forbidden", "NotFound"):
                    raise
                found = False
            if found:
                self._bucket = bucket
                break
        self._bucket_checked = True
        if self._bucket is not None:
            logger.info("camera: uploading photos to Firebase Storage bucket %s", self._bucket.name)
        elif self.upload_to == "storage":
            self._bucket_checked = False
            raise RuntimeError(f"no Firebase Storage bucket found (tried {', '.join(names)})")
        else:
            logger.info("camera: no Firebase Storage bucket found (tried %s) - "
                        "storing photos in Firestore instead", ", ".join(names))
        return self._bucket

    def _prune(self):
        cutoff = time.time() - self.keep_days * 86400
        for p in self.photo_dir.glob("*.jpg"):  # uploaded only; pending/ is never pruned
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass


def _project_id(credentials_path: Path) -> Optional[str]:
    try:
        return json.loads(Path(credentials_path).read_text()).get("project_id")
    except Exception:
        return None
