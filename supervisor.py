"""Keeps the timer running and undoes website changes that break it.

systemd runs this; this runs `python -m src.main` and watches it:

  exit 42   restart requested (website: restart, config/code edit, update)
            -> start it again at once
  exit 43   same, but supervisor.py itself changed -> exit so systemd starts
            the new supervisor
  crash     if it happens within VERIFY_SECONDS of a website change
            (data/update_state.json says "restarting"/"verifying"), put the old
            files / git commit back, note it for the website, start again.
            Otherwise wait a bit and start again.

Deliberately tiny, with no imports from src/ and no Firebase, so a broken
edit can't break the safety net. The website can't edit this file.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE = BASE / "data" / "update_state.json"
LOG = BASE / "data" / "logs" / "timer.log"
RESTART, FULL_RESTART = 42, 43
VERIFY_SECONDS = 90          # a little longer than src/remote.py's 60 s check
CRASH_WAIT_SECONDS = 10

child = None


def say(msg):
    print(f"[supervisor] {msg}", flush=True)


def read_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return None


def log_tail(n=30):
    try:
        return LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []


def rollback(state) -> str:
    rb = state.get("rollback") or {}
    if rb.get("type") == "git" and rb.get("commit"):
        r = subprocess.run(["git", "reset", "--hard", rb["commit"]], cwd=BASE,
                           capture_output=True, text=True)
        return f"git reset to {rb['commit']}" + ("" if r.returncode == 0 else f" FAILED: {r.stderr.strip()}")
    done = []
    for f in rb.get("files", []):
        target = BASE / f["path"]
        if f.get("backup"):
            shutil.copy2(BASE / f["backup"], target)
            done.append(f"restored {f['path']}")
        else:
            target.unlink(missing_ok=True)
            done.append(f"removed new file {f['path']}")
    return ", ".join(done) or "nothing to roll back"


def stop(signum, frame):
    if child and child.poll() is None:
        child.send_signal(signal.SIGTERM)  # the timer turns the relays OFF on SIGTERM
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
    sys.exit(0)


def main():
    global child
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    env = dict(os.environ, VFTIMER_SUPERVISED="1")
    while True:
        started = time.time()
        child = subprocess.Popen([sys.executable, "-m", "src.main"], cwd=BASE, env=env)
        code = child.wait()
        ran = time.time() - started
        if code == RESTART:
            say("restart requested")
            continue
        if code == FULL_RESTART:
            say("supervisor.py changed - exiting so systemd starts the new one")
            sys.exit(0)

        state = read_state()
        if state and state.get("state") in ("restarting", "verifying") and ran < VERIFY_SECONDS:
            note = rollback(state)
            say(f"timer crashed (exit {code}) {ran:.0f} s after a website change - {note}")
            state.update(state="rolled_back", rollback_note=note, exit_code=code,
                         log_tail=log_tail(), rolled_back_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
            STATE.write_text(json.dumps(state, indent=2))
            continue
        say(f"timer exited with code {code} after {ran:.0f} s - starting again in {CRASH_WAIT_SECONDS} s")
        time.sleep(CRASH_WAIT_SECONDS)


if __name__ == "__main__":
    main()
