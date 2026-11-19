"""Remote control from the website, through Firebase.

The website runs in Google Cloud and can't reach the Pi directly, so both sides
meet in Firestore under grows/<device_id>/:

  commands/<id>   written by the website: {type, args, status: "pending",
                  created_epoch}. The Pi runs it and sets status running ->
                  done / error (with `result` or `error`, and `progress` for
                  long ones). Commands older than 10 min are not run (the Pi was
                  offline when they were sent).
  live/status     the normal heartbeat (cloud_log.py) also carries `remote`:
                  code version, local edits, the last update/edit and which
                  USB webcams are plugged in. It's rewritten right after every
                  command so the website sees the result at once.

Commands
  ping
  device_on    {device, seconds | minutes}  force light/pump/fan on (pump capped
                                            at max_run_seconds), then back to schedule
  device_off   {device, minutes}            force off for a while
  device_auto  {device}                     back to the schedule now
  snap         {camera: plant|dashboard|both}
  swap_cameras                              plant <-> dashboard webcam, then restart
  get_config / set_config {file: base|local, text, restart}
  list_files / get_file {path | backup} / set_file {path, text, restart}
  get_diff                                  `git diff` of edits made on the Pi
  update_code  {branch, discard_local_changes, restart_anyway}
  get_logs, restart, reboot

Edits and updates are checked before they're saved (Python must compile,
config must load) and backed up. After the restart the new code has to stay up
for VERIFY_SECONDS; if it crashes before that, supervisor.py puts the old
files/commit back and the command is reported as rolled back.

Nothing here is needed for the timer itself: if Firebase is off, this idles.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from .config import BASE_FILE, LOCAL_FILE, deep_merge, read_yaml, validate_config

logger = logging.getLogger(__name__)

RESTART_EXIT_CODE = 42          # supervisor.py restarts the timer at once
FULL_RESTART_EXIT_CODE = 43     # supervisor.py exits too, so systemd restarts it (new supervisor)
COMMAND_MAX_AGE_SECONDS = 600
VERIFY_SECONDS = 60
POLL_SECONDS = 20
MAX_FILE_CHARS = 300_000
EDITABLE_SUFFIXES = (".py", ".yaml", ".yml", ".md", ".txt", ".sh", ".service", ".example", ".ini")
NOT_EDITABLE = {"supervisor.py"}  # the rollback safety net: change it through GitHub only
DEFAULT_REPO = "https://github.com/snobuta-ux/V2-VIP-VF-TIMER.git"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hhmm(iso) -> str:
    """'2026-11-19T12:10:00' -> '12:10' (the Pi's local time)."""
    return str(iso)[11:16] if iso and len(str(iso)) >= 16 else str(iso)


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


class RemoteControl:
    COMMANDS = {
        "ping", "device_on", "device_off", "device_auto", "snap", "swap_cameras",
        "get_config", "set_config", "list_files", "get_file", "set_file", "get_diff",
        "update_code", "get_logs", "restart", "reboot",
    }

    def __init__(self, controller, fb, cameras: dict, config: dict, base_dir: Path,
                 restart_fn: Optional[Callable[[int], None]] = None,
                 find_cameras: Optional[Callable[[], list]] = None):
        self.controller = controller
        self.fb = fb
        self.cameras = cameras or {}
        self.config = config
        self.base = Path(base_dir)
        self.restart_fn = restart_fn
        if find_cameras is None:
            from .camera import find_usb_cameras as find_cameras
        self.find_cameras = find_cameras
        self.repo_url = (config.get("remote") or {}).get("github_repo", DEFAULT_REPO)
        self.enabled = (config.get("remote") or {}).get("enabled", True)

        self.update_state_path = self.base / "data" / "update_state.json"
        self.started_epoch = time.time()
        self._running = False
        self._seen: set[str] = set()
        self._seen_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="remote-cmd")
        self._update_lock = threading.Lock()
        self._watch = None
        self._version_cache = (0.0, None)

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def db(self):
        return getattr(self.fb, "_db", None)

    def start(self) -> bool:
        if not self.enabled:
            logger.info("remote: disabled in config.yaml")
            return False
        if self.db is None:
            logger.info("remote: Firebase not connected, website control is off")
            return False
        self._running = True
        for target, name in ((self._poll_loop, "remote-poll"),
                             (self._verify_startup, "remote-verify")):
            threading.Thread(target=target, name=name, daemon=True).start()
        self._subscribe()
        logger.info("remote: listening for website commands at grows/%s/commands", self.fb.device_id)
        return True

    def stop(self):
        self._running = False
        try:
            if self._watch:
                self._watch.unsubscribe()
        except Exception:
            pass

    def _commands(self):
        return self.db.collection("grows").document(self.fb.device_id).collection("commands")

    def _pending(self):
        coll = self._commands()
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter
            return coll.where(filter=FieldFilter("status", "==", "pending"))
        except ImportError:
            return coll.where("status", "==", "pending")

    def _subscribe(self):
        try:
            self._watch = self._pending().on_snapshot(
                lambda docs, changes, read_time: [self._accept(d) for d in docs])
        except Exception as e:
            logger.warning("remote: live listener unavailable (%s); checking every %ds",
                           e, POLL_SECONDS)
            self._watch = None

    def _poll_loop(self):
        """Safety net in case the live listener drops."""
        while self._running:
            for _ in range(POLL_SECONDS if self._watch is None else 60):
                if not self._running:
                    return
                time.sleep(1)
            try:
                for d in self._pending().limit(20).get():
                    self._accept(d)
            except Exception as e:
                logger.debug("remote: command poll failed: %s", e)

    def _accept(self, doc):
        with self._seen_lock:
            if doc.id in self._seen:
                return
            self._seen.add(doc.id)
        self._pool.submit(self.run_command, doc.id, doc.to_dict() or {})

    # ── running commands ─────────────────────────────────────────────────────

    def set_command(self, cmd_id: Optional[str], status: str, result=None, error=None,
                    progress=None):
        if not cmd_id or self.db is None:
            return
        update = {"status": status, "updated_at": utc_now()}
        if status in ("done", "error", "expired"):
            update["finished_at"] = utc_now()
        if result is not None:
            update["result"] = result
        if error is not None:
            update["error"] = str(error)[:2000]
        if progress is not None:
            update["progress"] = str(progress)[:500]
        try:
            self._commands().document(cmd_id).set(update, merge=True)
        except Exception as e:
            logger.warning("remote: could not update command %s: %s", cmd_id, e)

    def run_command(self, cmd_id: str, data: dict):
        ctype = data.get("type")
        args = data.get("args") or {}
        created = data.get("created_epoch") or 0
        if created and time.time() - float(created) > COMMAND_MAX_AGE_SECONDS:
            self.set_command(cmd_id, "expired", error="The Pi was offline when this was sent; not run.")
            return
        if ctype not in self.COMMANDS:
            self.set_command(cmd_id, "error", error=f"Unknown command '{ctype}'")
            return
        self.set_command(cmd_id, "running")
        logger.info("website command: %s %s", ctype, json.dumps(args)[:200])
        try:
            result = getattr(self, f"cmd_{ctype}")(args, cmd_id)
            if result is not None:  # None = the command finishes itself later
                self.set_command(cmd_id, "done", result=result)
                self.fb.event("command", command=ctype, command_id=cmd_id, status="done",
                              message=str(result.get("message", ""))[:300], source="website")
        except Exception as e:
            logger.error("website command %s failed: %s", ctype, e)
            self.set_command(cmd_id, "error", error=e)
            self.fb.event("command", command=ctype, command_id=cmd_id, status="error",
                          message=str(e)[:300], source="website")
        finally:
            self.fb.write_status_soon()

    # ── commands: light / pump / fan ─────────────────────────────────────────

    def _device(self, args) -> str:
        name = str(args.get("device", ""))
        if name not in self.controller.devices:
            raise ValueError(f"unknown device '{name}' (this Pi has: "
                             f"{', '.join(self.controller.devices)})")
        return name

    @staticmethod
    def _seconds(args, default_minutes: float) -> float:
        if args.get("seconds") is not None:
            return float(args["seconds"])
        return float(args.get("minutes", default_minutes)) * 60

    def cmd_ping(self, args, cmd_id):
        return {"message": "pong", "uptime_s": int(time.time() - self.started_epoch)}

    def cmd_device_on(self, args, cmd_id):
        name = self._device(args)
        asked = self._seconds(args, 60)
        st = self.controller.set_override(name, True, asked, source="website")
        if st.get("simulated"):
            raise RuntimeError("GPIO isn't available on this Pi - the relay was not switched")
        until = _hhmm((st.get("manual_override") or {}).get("until"))
        capped = st.get("max_run_seconds") and asked > st["max_run_seconds"]
        return {"message": f"{name} ON until {until}, then back to its schedule"
                           + (f" (capped at the {st['max_run_seconds']:g} s safety limit)" if capped else ""),
                "device": name, "state": st}

    def cmd_device_off(self, args, cmd_id):
        name = self._device(args)
        st = self.controller.set_override(name, False, self._seconds(args, 60), source="website")
        until = _hhmm((st.get("manual_override") or {}).get("until"))
        return {"message": f"{name} OFF until {until}, then back to its schedule",
                "device": name, "state": st}

    def cmd_device_auto(self, args, cmd_id):
        name = self._device(args)
        st = self.controller.clear_override(name, source="website")
        return {"message": f"{name} back on its schedule (now {'ON' if st['on'] else 'OFF'})",
                "device": name, "state": st}

    # ── commands: cameras ────────────────────────────────────────────────────

    def cmd_snap(self, args, cmd_id):
        which = str(args.get("camera", "both"))
        names = list(self.cameras) if which == "both" else [which]
        missing = [n for n in names if n not in self.cameras]
        if missing or not names:
            raise ValueError(f"camera {', '.join(missing) or which} is not running on the Pi "
                             f"(running: {', '.join(self.cameras) or 'none'})")
        results = {}
        for n in names:
            self.set_command(cmd_id, "running", progress=f"Taking the {n} photo…")
            results[n] = self.cameras[n].snap("website")
        ok = [n for n, r in results.items() if r.get("ok")]
        failed = {n: r.get("error") for n, r in results.items() if not r.get("ok")}
        if not ok:
            raise RuntimeError("; ".join(f"{n}: {e}" for n, e in failed.items()))
        msg = f"Photo taken: {', '.join(ok)}"
        if any(not results[n].get("uploaded") for n in ok):
            msg += " (upload pending - will retry)"
        if failed:
            msg += ". Failed: " + "; ".join(f"{n}: {e}" for n, e in failed.items())
        return {"message": msg, "photos": {n: {k: r.get(k) for k in ("filename", "uploaded", "error")}
                                           for n, r in results.items()}}

    def cmd_swap_cameras(self, args, cmd_id):
        found = self.find_cameras()
        if len(found) < 2:
            raise RuntimeError(f"Need two USB webcams plugged in to swap; found {len(found)}.")
        current = {n: c.device for n, c in self.cameras.items()}
        by_dev = {c["dev"]: c["port"] for c in found}
        plant = by_dev.get(current.get("plant") or "", found[0]["port"])
        dash = by_dev.get(current.get("dashboard") or "",
                          next(c["port"] for c in found if c["port"] != plant))
        local = read_yaml(self.base / LOCAL_FILE)
        local = deep_merge(local, {"cameras": {"plant": {"device": dash},
                                               "dashboard": {"device": plant}}})
        text = yaml.safe_dump(local, sort_keys=False)
        return self._save_and_restart(cmd_id, {LOCAL_FILE: text}, "config",
                                      f"Cameras swapped: plant = USB port {dash}, "
                                      f"dashboard = USB port {plant}")

    # ── commands: config + code editor ───────────────────────────────────────

    def cmd_get_config(self, args, cmd_id):
        base_p, local_p = self.base / BASE_FILE, self.base / LOCAL_FILE
        from .config import load_config
        return {
            "message": "Config loaded",
            "config_yaml": base_p.read_text(encoding="utf-8"),
            "local_yaml": local_p.read_text(encoding="utf-8") if local_p.exists() else "",
            "effective_yaml": yaml.safe_dump(load_config(self.base), sort_keys=False),
            "backups": self._backups()[:30],
        }

    def cmd_set_config(self, args, cmd_id):
        which = args.get("file", "base")
        if which not in ("base", "local"):
            raise ValueError("file must be 'base' or 'local'")
        target = BASE_FILE if which == "base" else LOCAL_FILE
        return self.cmd_set_file({"path": target, "text": args.get("text", ""),
                                  "restart": args.get("restart", True)}, cmd_id)

    def _editable_files(self) -> list[str]:
        files = []
        try:
            r = self._git("ls-files")
            files = r.stdout.splitlines() if r.returncode == 0 else []
        except Exception:
            pass
        if not files:  # not a git checkout: list what's there
            files = [p.relative_to(self.base).as_posix() for p in self.base.rglob("*")
                     if p.is_file() and not any(part in ("venv", ".venv", "data", ".git", "__pycache__")
                                                for part in p.relative_to(self.base).parts)]
        out = {f for f in files if f.endswith(EDITABLE_SUFFIXES) and f not in NOT_EDITABLE
               and "credentials" not in f}
        out.add(LOCAL_FILE)
        return sorted(out)

    def _check_path(self, path: str) -> Path:
        path = str(path or "").strip().lstrip("/")
        if path not in self._editable_files():
            raise ValueError(f"'{path}' can't be edited from the website")
        return self.base / path

    def _backups(self) -> list[str]:
        d = self.base / "data" / "code_backups"
        return sorted((p.name for p in d.iterdir() if p.is_file()), reverse=True) if d.exists() else []

    def cmd_list_files(self, args, cmd_id):
        changed = set(self._version(fresh=True).get("local_changes") or [])
        files = []
        for f in self._editable_files():
            p = self.base / f
            files.append({"path": f, "bytes": p.stat().st_size if p.exists() else 0,
                          "exists": p.exists(), "edited_on_pi": f in changed})
        return {"message": f"{len(files)} files", "files": files, "backups": self._backups()[:50],
                "version": self._version()}

    def cmd_get_file(self, args, cmd_id):
        if args.get("backup"):
            name = Path(str(args["backup"])).name
            p = self.base / "data" / "code_backups" / name
            if not p.exists():
                raise ValueError(f"no backup called {name}")
            return {"message": f"Backup {name}", "backup": name, "text": p.read_text(encoding="utf-8")}
        p = self._check_path(args.get("path"))
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        return {"message": f"Loaded {args.get('path')}", "path": args.get("path"), "text": text}

    def cmd_get_diff(self, args, cmd_id):
        r = self._git("diff")
        return {"message": "Edits made on the Pi (git diff)", "diff": r.stdout[-200_000:] or "(no edits)",
                "version": self._version(fresh=True)}

    def cmd_set_file(self, args, cmd_id):
        rel = str(args.get("path", "")).strip().lstrip("/")
        self._check_path(rel)
        text = str(args.get("text", ""))
        if len(text) > MAX_FILE_CHARS:
            raise ValueError(f"file too large (max {MAX_FILE_CHARS} characters)")
        self._check_content(rel, text)
        note = f"Saved {rel}"
        if args.get("restart", True) is False:
            self._write_files({rel: text})
            return {"message": note + ". Takes effect at the next restart."}
        return self._save_and_restart(cmd_id, {rel: text}, "file", note)

    def _check_content(self, rel: str, text: str):
        if rel.endswith(".py"):
            try:
                compile(text, rel, "exec")
            except SyntaxError as e:
                raise ValueError(f"Not saved - Python syntax error in {rel} line {e.lineno}: {e.msg}")
        if rel.endswith((".yaml", ".yml")):
            try:
                parsed = yaml.safe_load(text) if text.strip() else {}
            except yaml.YAMLError as e:
                raise ValueError(f"Not saved - YAML syntax error: {e}")
            if rel in (BASE_FILE, LOCAL_FILE):
                if parsed is not None and not isinstance(parsed, dict):
                    raise ValueError("Not saved - config must be a YAML mapping (key: value)")
                base = parsed if rel == BASE_FILE else read_yaml(self.base / BASE_FILE)
                local = parsed if rel == LOCAL_FILE else read_yaml(self.base / LOCAL_FILE)
                errors = validate_config(deep_merge(base or {}, local or {}))
                if errors:
                    raise ValueError("Not saved - the timer wouldn't start with this config: "
                                     + "; ".join(errors))

    def _write_files(self, files: dict) -> list[dict]:
        """Back up and replace files. Returns rollback info for supervisor.py."""
        backup_dir = self.base / "data" / "code_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        rollback = []
        for rel, text in files.items():
            target = self.base / rel
            backup = None
            if target.exists():
                backup = backup_dir / f"{_stamp()}_{rel.replace('/', '__')}"
                shutil.copy2(target, backup)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(target)
            rollback.append({"path": rel,
                             "backup": backup.relative_to(self.base).as_posix() if backup else None})
            logger.warning("%s edited from the website (backup %s)", rel,
                           backup.name if backup else "none - new file")
        return rollback

    def _save_and_restart(self, cmd_id, files: dict, kind: str, note: str):
        rollback = self._write_files(files)
        self.write_update_state({
            "state": "restarting", "kind": kind, "command_id": cmd_id, "note": note,
            "files": list(files), "started_at": utc_now(),
            "rollback": {"type": "files", "files": rollback},
        })
        self.set_command(cmd_id, "running", progress=f"{note}. Restarting the timer…")
        self.request_restart()
        return None

    # ── commands: GitHub update ──────────────────────────────────────────────

    def cmd_update_code(self, args, cmd_id):
        if not self._update_lock.acquire(blocking=False):
            raise RuntimeError("Another update is already running; wait for it to finish.")
        try:
            return self._update_code(args, cmd_id)
        finally:
            self._update_lock.release()

    def _update_code(self, args, cmd_id):
        def progress(msg):
            logger.info("update: %s", msg)
            self.set_command(cmd_id, "running", progress=msg)

        if not self._is_git_repo():
            raise RuntimeError("The timer folder on the Pi isn't a git checkout. Clone it with "
                               f"git clone {self.repo_url} and run install.sh.")
        branch = str(args.get("branch") or "").strip()
        if branch and not all(c.isalnum() or c in "-_./" for c in branch):
            raise ValueError("invalid branch name")
        current = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        branch = branch or (current if current != "HEAD" else "main")

        dirty = self._version(fresh=True).get("local_changes") or []
        if dirty:
            if not args.get("discard_local_changes"):
                raise RuntimeError("The Pi has edits made from the website to: " + ", ".join(dirty[:10])
                                   + ". Tick 'discard Pi edits' to put them aside (git stash) and update.")
            progress("Putting the Pi's edits aside (git stash)…")
            self._git("stash", "push", "-m", f"website update {utc_now()}")

        progress(f"Fetching {branch} from GitHub…")
        r = self._git("fetch", "origin", branch, timeout=180)
        if r.returncode:
            raise RuntimeError(f"git fetch failed: {r.stderr.strip()[:500]}")
        from_commit = self._git("rev-parse", "--short", "HEAD").stdout.strip()
        if branch != current:
            r = self._git("checkout", branch)
            if r.returncode:
                r = self._git("checkout", "-b", branch, "--track", f"origin/{branch}")
            if r.returncode:
                raise RuntimeError(f"could not switch to {branch}: {r.stderr.strip()[:400]}")
        r = self._git("merge", "--ff-only", f"origin/{branch}")
        if r.returncode:
            raise RuntimeError("The Pi's copy has diverged from GitHub (can't fast-forward): "
                               + r.stderr.strip()[:400])
        to_commit = self._git("rev-parse", "--short", "HEAD").stdout.strip()
        commits = self._git("log", "--oneline", f"{from_commit}..HEAD").stdout.splitlines()[:30]
        if to_commit == from_commit and not args.get("restart_anyway"):
            return {"message": f"Already up to date ({to_commit} on {branch})", "to": to_commit}

        changed = self._git("diff", "--name-only", from_commit, "HEAD").stdout.splitlines()
        if "requirements.txt" in changed:
            progress("requirements.txt changed - installing packages…")
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                               cwd=self.base, capture_output=True, text=True, timeout=900)
            if r.returncode:
                logger.warning("pip install had problems: %s", r.stderr[-800:])

        progress("Checking the new code compiles…")
        r = subprocess.run([sys.executable, "-m", "compileall", "-q", "-f", "src"], cwd=self.base,
                           capture_output=True, text=True, timeout=180)
        if r.returncode:
            self._git("reset", "--hard", from_commit)
            raise RuntimeError("New code has syntax errors, kept the old version:\n"
                               + (r.stdout + r.stderr)[-1500:])

        self.write_update_state({
            "state": "restarting", "kind": "code", "command_id": cmd_id, "branch": branch,
            "from_commit": from_commit, "to_commit": to_commit, "commits": commits,
            "started_at": utc_now(), "rollback": {"type": "git", "commit": from_commit},
        })
        progress(f"Updated {from_commit} → {to_commit}. Restarting…")
        # A new supervisor.py only takes effect if the whole service restarts
        self.request_restart(FULL_RESTART_EXIT_CODE if "supervisor.py" in changed else RESTART_EXIT_CODE)
        return None

    # ── commands: system ─────────────────────────────────────────────────────

    def cmd_get_logs(self, args, cmd_id):
        self.fb.write_status_soon()
        log = self.base / "data" / "logs" / "timer.log"
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-500:] if log.exists() else []
        return {"message": f"{len(lines)} log lines", "lines": lines}

    def cmd_restart(self, args, cmd_id):
        self.set_command(cmd_id, "done", result={"message": "Restarting the timer software…"})
        self.request_restart()
        return None

    def cmd_reboot(self, args, cmd_id):
        self.set_command(cmd_id, "running", progress="Rebooting the Pi…")
        r = subprocess.run(["sudo", "-n", "/sbin/reboot"], capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            raise RuntimeError("Reboot not allowed: " + (r.stderr.strip() or "sudo needs a password")
                               + ". Run install.sh again on the Pi to allow it.")
        self.set_command(cmd_id, "done", result={"message": "Reboot started"})
        return None

    def request_restart(self, code: int = RESTART_EXIT_CODE):
        if self.restart_fn is None:
            raise RuntimeError("restart is not available")

        def go():
            time.sleep(1.5)  # let the command status reach Firestore first
            self.restart_fn(code)

        threading.Thread(target=go, daemon=True).start()

    # ── after a restart: did the new code/config stay up? ────────────────────

    def read_update_state(self) -> Optional[dict]:
        try:
            return json.loads(self.update_state_path.read_text())
        except Exception:
            return None

    def write_update_state(self, state: dict):
        self.update_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.update_state_path.write_text(json.dumps(state, indent=2))

    def _verify_startup(self, wait: float = VERIFY_SECONDS):
        state = self.read_update_state()
        if not state:
            return
        cmd_id = state.get("command_id")
        if state.get("state") == "rolled_back":
            self.set_command(cmd_id, "error", error=(
                f"The timer crashed after the change, so the old version was put back "
                f"({state.get('rollback_note', '')}). Last log lines:\n"
                + "\n".join(state.get("log_tail", [])[-15:])))
            self.fb.event("rollback", message=state.get("note") or "change rolled back",
                          change=state.get("kind"), source="supervisor")
            self.write_update_state({**state, "state": "reported"})
            return
        if state.get("state") != "restarting":
            return
        self.write_update_state({**state, "state": "verifying", "verifying_since": utc_now()})
        what = f"New code {state.get('to_commit')}" if state.get("kind") == "code" else "The change"
        self.set_command(cmd_id, "running",
                         progress=f"{what} started. Checking it stays up for {int(wait)} s…")
        self.fb.write_status_soon()
        time.sleep(wait)
        if not self._running:
            return
        commit = self._version(fresh=True).get("commit")
        msg = (f"Update OK: now running {commit}." if state.get("kind") == "code"
               else f"{state.get('note') or 'Change saved'}. The timer restarted and stayed up.")
        self.write_update_state({**state, "state": "success", "finished_at": utc_now()})
        self.set_command(cmd_id, "done", result={
            "message": msg, "from": state.get("from_commit"), "to": commit,
            "commits": state.get("commits", [])})
        self.fb.event("command", command=state.get("kind"), command_id=cmd_id, status="done",
                      message=msg, source="website")
        self.fb.write_status_soon()

    # ── status for the heartbeat ─────────────────────────────────────────────

    def status(self) -> dict:
        state = self.read_update_state() or {}
        try:
            usb = self.find_cameras()
        except Exception:
            usb = []
        return {
            "enabled": self._running,
            "version": self._version(),
            "last_change": {k: state.get(k) for k in
                            ("state", "kind", "note", "from_commit", "to_commit", "started_at",
                             "finished_at") if state.get(k)},
            "usb_cameras": usb,
            "cameras": {n: c.status() for n, c in self.cameras.items()},
        }

    def _git(self, *args, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.base, capture_output=True, text=True,
                              timeout=timeout)

    def _is_git_repo(self) -> bool:
        try:
            return (self.base / ".git").exists() and \
                self._git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
        except Exception:
            return False

    def _version(self, fresh: bool = False) -> dict:
        ts, cached = self._version_cache
        if cached is not None and not fresh and time.time() - ts < 300:
            return cached
        if not self._is_git_repo():
            value = {"git": False}
        else:
            try:
                value = {
                    "git": True,
                    "commit": self._git("rev-parse", "--short", "HEAD").stdout.strip(),
                    "branch": self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
                    "message": self._git("log", "-1", "--format=%s").stdout.strip()[:200],
                    "date": self._git("log", "-1", "--format=%cI").stdout.strip(),
                    "local_changes": [l[3:] for l in self._git(
                        "status", "--porcelain", "--untracked-files=no").stdout.splitlines()][:30],
                }
            except Exception as e:
                value = {"git": True, "error": str(e)}
        self._version_cache = (time.time(), value)
        return value
