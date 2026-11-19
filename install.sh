#!/usr/bin/env bash
# One-time setup on the Raspberry Pi. Run from the project folder:
#   bash install.sh
# Safe to run again after pulling updates.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE=vf-timer
cd "$PROJECT_DIR"

# Always follow GitHub's main branch. Older setups were on a feature branch
# (or a clone of just that branch), so `git pull` never got the new code.
if [ -d .git ] && [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
    echo "== Switching this Pi to the main branch"
    if git remote set-branches origin '*' && git fetch -q origin \
        && git checkout -q -B main origin/main && git branch -q -u origin/main main; then
        echo "   now on main: $(git log -1 --oneline)"
        exec bash "$PROJECT_DIR/install.sh"   # run the main branch's installer
    else
        echo "   could not switch to main (offline?) - continuing on $(git rev-parse --abbrev-ref HEAD)"
    fi
fi

echo "== Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip fswebcam
# GPIO library: rpi-lgpio works on Pi 4 and Pi 5; fall back to classic RPi.GPIO
sudo apt-get install -y -qq python3-rpi-lgpio || sudo apt-get install -y -qq python3-rpi.gpio

echo "== Creating Python environment"
if [ ! -d venv ]; then
    python3 -m venv --system-site-packages venv
fi
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "   created .env (edit it to set API_KEY if you want one)"
fi

# Let this user drive GPIO without sudo
sudo usermod -aG gpio "$USER" || true
# ...and use the webcam
sudo usermod -aG video "$USER" || true

# Let the website's "Reboot Pi" button work (only /sbin/reboot, nothing else)
echo "$USER ALL=(ALL) NOPASSWD: /sbin/reboot" | sudo tee /etc/sudoers.d/vf-timer-reboot > /dev/null
sudo chmod 440 /etc/sudoers.d/vf-timer-reboot

echo "== Installing the $SERVICE service"
sed -e "s|__USER__|$USER|g" -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    deploy/vf-timer.service | sudo tee /etc/systemd/system/$SERVICE.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE
sudo systemctl restart $SERVICE

echo
echo "Done. The timer is running and will start on every boot."
echo "  Pi clock:      $(date)   <- schedules use this time; fix with: sudo timedatectl set-timezone <zone>"
echo "  Live log:      journalctl -u $SERVICE -f"
echo "  Status:        curl http://localhost:8080/api/status"
