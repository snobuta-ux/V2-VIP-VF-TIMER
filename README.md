# VIP Vertical Farm — Timer Controller (V2)

A plain timer version of the [VIP Vertical Farm](https://github.com/CoolGuy2982/VIP-Vertical-Farm) grower for a separate trial. There's no AI: the grow light, water pump and fan each run on a fixed schedule that you set in `config.yaml`.

- **Light**: on/off at set clock times (default 06:00–22:00)
- **Pump**: runs N seconds every M minutes (default 5 s every 6 h)
- **Fan**: runs N seconds every M minutes (default 10 min every 30 min). Turned off in `config.yaml` until it's wired

All three are switched by GPIO relays on a Raspberry Pi. Schedules depend only on the clock, so a reboot never double-waters or skips a cycle.

## Hardware

Raspberry Pi 4 Model B with a 4-channel 5V relay board (Songle SRD-05VDC-SL-C).

| Relay board pin | Relay | Device | Pi physical pin (BOARD) | GPIO (BCM) |
|---|---|---|---|---|
| VCC | | power | **2** (5V) | |
| GND | | ground | **6** (GND) | |
| IN1 | K1 (far left) | Water pump | **11** | GPIO17 |
| IN2 | K2 | Fan (disabled until wired) | **13** | GPIO27 |
| IN3 | K3 | spare | **15** | GPIO22 |
| IN4 | K4 (far right) | Grow light | **16** | GPIO23 |

Leave the **JD-VCC jumper** on the relay board in place. `config.yaml` uses the physical (BOARD) pin numbers.

Each device's power goes through its relay's **COM** and **NO** (normally open) screw terminals, so a device is OFF whenever the Pi is off or the program isn't running. Check with a multimeter or the board's markings which terminal is which, because if you use **NC** instead the device runs backwards (on when it should be off).

> **Active-Low:** most relay boards switch ON when the pin goes LOW. If your relays come on when they should be off, set `relay.active_low: false` in `config.yaml`.

> **Mains voltage:** if the light, pump or fan runs on 120 V, use a relay board rated for it and have someone check the wiring before you plug it in.

To use different pins, change `pin:` under each device in `config.yaml`.

## Install on a new Raspberry Pi

1. Flash **Raspberry Pi OS** with [Raspberry Pi Imager](https://www.raspberrypi.com/software/). In its settings, turn on SSH and set up Wi-Fi.
2. SSH in and clone this repo:
   ```bash
   git clone https://github.com/snobuta-ux/V2-VIP-VF-TIMER.git
   cd V2-VIP-VF-TIMER
   ```
3. Set the Pi's timezone, because schedules use its clock:
   ```bash
   sudo timedatectl set-timezone America/Indiana/Indianapolis
   ```
4. Run the installer. It installs the dependencies and starts the `vf-timer` service, which then also starts on every boot:
   ```bash
   bash install.sh
   ```

### Test the wiring

```bash
sudo systemctl stop vf-timer
venv/bin/python test_hardware.py          # clicks pump and light one by one
sudo systemctl start vf-timer
```

## Change the schedule

Edit `config.yaml`, then restart:

```bash
nano config.yaml
sudo systemctl restart vf-timer
```

| Type | Settings | Example |
|---|---|---|
| `daily` | `on_time`, `off_time` | light on 06:00, off 22:00 |
| `interval` | `every_minutes`, `run_seconds`, `start_at` | pump 60 s every 360 min from 06:00 |
| `times` | `at` (list), `run_seconds` | pump 5 s at `["07:00", "19:00"]` |
| `always_on` | none | fan always running |
| `off` | none | device disabled |

`max_run_seconds` is a safety cut-off. If a device stays on longer than that, it's forced off. It's set to 90 s for the pump, so a typo can't flood the tray.

For a quick test, try `every_minutes: 2` and `run_seconds: 2` on the pump. Remember to change it back.

## Check on it

```bash
journalctl -u vf-timer -f                 # live log (also saved in data/logs/timer.log)
curl http://<pi-ip>:8080/api/status       # current state + next change for each device
```

## Firebase logging

Everything the timer does is logged to Firestore under `grows/<device_id>` (`device_id: farm2` in `config.yaml`):

| Path | What's in it |
|---|---|
| `grows/farm2` | farm name, pins, schedules, start time, code version |
| `grows/farm2/live/status` | heartbeat every 60 s: `online`, each device's state and next change, Pi clock vs `server_time`, CPU temp, disk, uptime |
| `grows/farm2/live/logs` | the last 200 log lines |
| `grows/farm2/events/*` | one doc per event: `switch` (relay on/off, with `source` and `ran_seconds`), `override`, `override_cleared`, `override_ended`, `safety_cutoff`, `safety_cleared`, `warning`, `error`, `startup`, `shutdown` |

Every doc has `server_time` (Firebase's clock), so sort by that: the Pi's own clock can be wrong.

Setup: put the service account key in the project folder as `firebase-credentials.json` (Firebase console → Project settings → Service accounts → Generate new private key), then run `bash install.sh`. **Never commit that file.** It's in `.gitignore`, and this repo is public.

If Firebase can't be reached, the timer keeps running and keeps the events in memory until the connection is back. Set `firebase.enabled: false` to turn logging off.

## Webcam photos

Plug a USB webcam into any USB port. The timer finds it by itself, and if it's unplugged or moved to another port it finds it again before the next photo.

By default it takes a photo every hour from 06:00 to 21:00, skipping any while the grow light is off (`camera:` in `config.yaml`). Files are named `<group>_<device_id>_<date>_<time>.jpg`, e.g. `timer_farm2_2026-11-12_09-00-00.jpg`, and uploaded to:

- Firebase Storage: `grows/farm2/photos/<filename>`, with an info doc (time, light on/off, download `url`) in Firestore `grows/farm2/photos`.
- If the Firebase project has no Storage bucket, the photo is stored in that Firestore doc instead (field `jpeg`).

Photos are saved in `data/photos/` first. If the Pi is offline they wait in `data/photos/pending/` and upload once it's back.

Test it: `curl -X POST http://localhost:8080/api/camera/snap`. Camera status: `curl http://localhost:8080/api/camera`.

## API

| Method | Endpoint | What it does |
|---|---|---|
| GET | `/api/status` | Every device: on/off, schedule, next change, recent events |
| GET | `/api/health` | `{"status": "ok"}` |
| POST | `/api/{device}/on` | Force on. Body `{"seconds": 10}` or `{"minutes": 5}` (default 60 min, pump capped at `max_run_seconds`) |
| POST | `/api/{device}/off` | Force off for a while (same body, default 60 min) |
| GET | `/api/camera` | Webcam found, next photo, last photo, uploads waiting |
| POST | `/api/camera/snap` | Take and upload a photo now |
| POST | `/api/{device}/auto` | Cancel the manual override and go back to the schedule |

`{device}` is `light`, `pump` or `fan`. Manual overrides always end on their own and return to the schedule.

```bash
curl -X POST http://<pi-ip>:8080/api/pump/on -H "Content-Type: application/json" -d '{"seconds": 5}'
```

If you set `API_KEY` in `.env`, add `-H "X-API-Key: <your key>"` to POST requests.

## Update the Pi after changing code

```bash
cd ~/V2-VIP-VF-TIMER && git pull && bash install.sh
```

## Develop on a laptop

Without a Pi, the relays are simulated: they only log what they would do.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest
python -m src.main
```

## Project layout

```
config.yaml          pins + schedules (the only file you normally edit)
src/schedules.py     schedule types: "should this be on right now?"
src/relays.py        GPIO relay on/off (simulated off-Pi)
src/controller.py    1-second loop, manual overrides, safety cut-off
src/api_server.py    REST API
src/cloud_log.py     Firebase logging (events, status heartbeat, log lines)
src/camera.py        USB webcam photos -> Firebase
src/main.py          starts everything
test_hardware.py     relay wiring test
install.sh           Pi setup + systemd service
```
