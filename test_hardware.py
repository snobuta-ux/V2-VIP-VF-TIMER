"""Click each relay on and off so you can check the wiring.

Stop the service first so it isn't holding the pins:
    sudo systemctl stop vf-timer
    venv/bin/python test_hardware.py            # all devices
    venv/bin/python test_hardware.py pump       # just one
"""

import sys
import time
from pathlib import Path

import yaml

from src.relays import GPIO, Relay, cleanup_gpio, init_gpio

ON_SECONDS = {"pump": 2}  # keep the pump test short; everything else gets 5 s
DEFAULT_ON_SECONDS = 5


def main():
    config = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text())
    active_low = config.get("relay", {}).get("active_low", True)
    devices = config.get("devices", {})
    wanted = sys.argv[1:] or list(devices)

    if GPIO is None:
        print("RPi.GPIO not found - run this on the Raspberry Pi.")
        sys.exit(1)

    init_gpio()
    relays = []
    try:
        for name in wanted:
            if name not in devices:
                print(f"unknown device {name!r} (config has: {', '.join(devices)})")
                continue
            relay = Relay(name, devices[name]["pin"], active_low=active_low)
            relays.append(relay)
            seconds = ON_SECONDS.get(name, DEFAULT_ON_SECONDS)
            input(f"\n{name} (pin {relay.pin}): press Enter to turn ON for {seconds}s...")
            relay.set(True)
            print(f"  {name} ON - you should hear the relay click and see it run")
            time.sleep(seconds)
            relay.set(False)
            print(f"  {name} OFF")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        cleanup_gpio(relays)
    print("\ndone")


if __name__ == "__main__":
    main()
