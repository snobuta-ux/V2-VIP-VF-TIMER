"""Send everything the timer does to Firebase (Firestore).

Layout (device_id from config.yaml, default "farm2"):

  grows/<device_id>                    farm info: name, type, pins, schedules, started_at
  grows/<device_id>/live/status        heartbeat every `heartbeat_seconds`: online, every
                                       device's state + next change, Pi clock vs server
                                       clock, CPU temp, disk, uptime, code version
  grows/<device_id>/live/logs          the last 200 log lines
  grows/<device_id>/events/<auto id>   one doc per event: a relay switching on/off, manual
                                       overrides from the API, safety cut-offs, warnings,
                                       errors, startup and shutdown

Every doc gets `server_time` (Firestore's clock) as well as `pi_time`, so the
log is still right when the Pi's clock is wrong.

Firebase must never stop the timer. If firebase-admin isn't installed, the
credentials are missing or the Pi is offline, this logs a warning and the
timer keeps running. Events that couldn't be sent are kept in memory (up to
5000) and sent once the connection is back.
"""

import logging
import queue
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Our own log lines and the Google libraries' are never sent, otherwise one
# failed write would log an error, which would try another write, and so on.
_SKIP_LOGGERS = ("src.cloud_log", "google", "grpc", "urllib3", "firebase_admin",
                 "cachecontrol", "httpx", "httpcore", "uvicorn.access")

LOG_LINES_KEPT = 200
MAX_PENDING_EVENTS = 5000
RETRY_SECONDS = 30


def _server_timestamp():
    from firebase_admin import firestore
    return firestore.SERVER_TIMESTAMP


class FirebaseLogger:
    def __init__(self, config: dict, base_dir: Path, status_fn: Callable[[], dict],
                 farm_info: Optional[dict] = None, client=None,
                 server_timestamp: Optional[Callable] = None):
        cfg = config or {}
        self.enabled = cfg.get("enabled", True)
        self.device_id = str(cfg.get("device_id", "farm2"))
        self.database_id = cfg.get("database_id") or "(default)"
        self.heartbeat_seconds = max(10, float(cfg.get("heartbeat_seconds", 60)))
        cred = cfg.get("credentials_file", "firebase-credentials.json")
        self.credentials_path = Path(cred) if Path(cred).is_absolute() else base_dir / cred

        self.base_dir = base_dir
        self.status_fn = status_fn
        self.farm_info = farm_info or {}
        self._db = client
        self._server_ts = server_timestamp or _server_timestamp

        self._events: deque = deque(maxlen=MAX_PENDING_EVENTS)
        self._wake = queue.Queue()
        self._log_lines: deque = deque(maxlen=LOG_LINES_KEPT)
        self._logs_changed = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self._version = _git_version(base_dir)
        self.last_error: Optional[str] = None
        self.last_success: Optional[float] = None

    # ── setup ────────────────────────────────────────────────────────────────

    def start(self) -> bool:
        if not self.enabled:
            logger.info("firebase: disabled in config.yaml")
            return False
        if self._db is None:
            try:
                self._db = self._connect()
            except Exception as e:
                logger.warning("firebase: NOT logging to Firebase (%s). The timer still runs.", e)
                return False
        logger.info("firebase: logging to grows/%s (database %s)", self.device_id, self.database_id)
        self.event("startup", message="timer started", version=self._version)
        self._thread = threading.Thread(target=self._run, name="firebase", daemon=True)
        self._thread.start()
        return True

    def _connect(self):
        if not self.credentials_path.exists():
            raise RuntimeError(f"credentials file not found: {self.credentials_path}")
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore
        except ImportError:
            raise RuntimeError("firebase-admin is not installed (run: bash install.sh)")

        app_name = f"vf-timer-{self.device_id}"
        try:
            app = firebase_admin.get_app(app_name)
        except ValueError:
            app = firebase_admin.initialize_app(
                credentials.Certificate(str(self.credentials_path)), name=app_name)
        if self.database_id == "(default)":
            return firestore.client(app)
        return firestore.client(app, database_id=self.database_id)

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── called by the rest of the program ────────────────────────────────────

    def event(self, kind: str, **fields):
        """Queue one event doc. Never blocks and never raises."""
        doc = {"type": kind, **fields}
        doc.setdefault("pi_time", datetime.now().astimezone().isoformat(timespec="seconds"))
        doc.setdefault("epoch", time.time())
        with self._lock:
            self._events.append(doc)
        self._wake.put_nowait(None)

    def controller_event(self, ev: dict):
        """Listener for Controller events (relay switches, overrides, cut-offs)."""
        ev = dict(ev)
        kind = ev.pop("type", "switch")
        if "time" in ev:
            ev["pi_time"] = ev.pop("time")
        self.event(kind, **ev)

    def add_log_line(self, line: str, level: int, name: str):
        with self._lock:
            self._log_lines.append(line)
            self._logs_changed = True
        if level >= logging.WARNING:
            self.event("error" if level >= logging.ERROR else "warning",
                       message=line, logger=name)

    def stop(self, timeout: float = 10):
        if not self.active:
            return
        self.event("shutdown", message="timer stopped")
        self._stop.set()
        self._wake.put_nowait(None)
        self._thread.join(timeout=timeout)

    # ── background thread ────────────────────────────────────────────────────

    def _run(self):
        farm_info_written = False
        next_beat = 0.0
        while True:
            stopping = self._stop.is_set()
            if not farm_info_written:
                farm_info_written = self._safe(self._write_farm_info)
            ok = farm_info_written and self._safe(self._flush_events)
            now = time.monotonic()
            if ok and (now >= next_beat or stopping):
                if self._safe(lambda: self._write_status(online=not stopping)):
                    self._safe(self._write_logs)
                next_beat = now + self.heartbeat_seconds
            if stopping:
                return
            if not ok:
                # Offline: wait before retrying instead of hammering the network.
                # Only a stop request cuts this short; new events just queue up.
                self._stop.wait(RETRY_SECONDS)
                continue
            try:
                self._wake.get(timeout=max(0.5, next_beat - time.monotonic()))
                while True:  # one wake-up is enough for any number of new events
                    self._wake.get_nowait()
            except queue.Empty:
                pass

    def _safe(self, fn) -> bool:
        try:
            fn()
            if self.last_error:
                logger.info("firebase: connection OK again")
            self.last_error = None
            self.last_success = time.time()
            return True
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if msg != self.last_error:
                logger.warning("firebase: write failed (%s). Will retry; events are kept.", msg)
            self.last_error = msg
            return False

    def _grow(self):
        return self._db.collection("grows").document(self.device_id)

    def _write_farm_info(self):
        self._grow().set({
            **self.farm_info,
            "device_id": self.device_id,
            "kind": "timer",
            "hostname": socket.gethostname(),
            "started_at": self._started_at,
            "version": self._version,
            "updated_at": self._server_ts(),
        }, merge=True)

    def _flush_events(self):
        events = self._grow().collection("events")
        while True:
            with self._lock:
                if not self._events:
                    return
                doc = self._events[0]
            events.add({**doc, "server_time": self._server_ts()})
            with self._lock:
                if self._events and self._events[0] is doc:
                    self._events.popleft()

    def _write_status(self, online: bool = True):
        try:
            status = self.status_fn()
        except Exception as e:
            status = {"error": f"could not read status: {e}"}
        with self._lock:
            pending = len(self._events)
        self._grow().collection("live").document("status").set({
            **status,
            "online": online,
            "pi_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "pi_epoch": time.time(),
            "server_time": self._server_ts(),
            "started_at": self._started_at,
            "heartbeat_seconds": self.heartbeat_seconds,
            "pending_events": pending,
            "host": _host_stats(self.base_dir),
            "version": self._version,
        })

    def _write_logs(self):
        with self._lock:
            if not self._logs_changed:
                return
            lines = list(self._log_lines)
            self._logs_changed = False
        self._grow().collection("live").document("logs").set({
            "lines": lines,
            "server_time": self._server_ts(),
        })


class CloudLogHandler(logging.Handler):
    """Copies log lines into FirebaseLogger (all lines into live/logs,
    warnings and errors also as event docs)."""

    def __init__(self, fb: FirebaseLogger, level=logging.INFO):
        super().__init__(level)
        self.fb = fb
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord):
        if record.name.startswith(_SKIP_LOGGERS):
            return
        try:
            self.fb.add_log_line(self.format(record), record.levelno, record.name)
        except Exception:
            pass


# ── helpers ──────────────────────────────────────────────────────────────────

def _git_version(base_dir: Path) -> dict:
    def git(*args):
        try:
            return subprocess.run(["git", *args], cwd=base_dir, capture_output=True,
                                  text=True, timeout=5).stdout.strip() or None
        except Exception:
            return None
    return {"commit": git("rev-parse", "--short", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "message": git("log", "-1", "--format=%s")}


def _host_stats(base_dir: Path) -> dict:
    stats = {}
    try:
        stats["cpu_temp_c"] = round(int(Path("/sys/class/thermal/thermal_zone0/temp")
                                        .read_text()) / 1000, 1)
    except Exception:
        pass
    try:
        stats["disk_free_gb"] = round(shutil.disk_usage(base_dir).free / 1e9, 2)
    except Exception:
        pass
    try:
        stats["uptime_hours"] = round(float(Path("/proc/uptime").read_text().split()[0]) / 3600, 2)
    except Exception:
        pass
    return stats
