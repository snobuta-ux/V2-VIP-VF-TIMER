"""GPIO relay control for the light, pump and fan.

All three devices are switched the same way: a Raspberry Pi GPIO pin drives a
relay module channel (BOARD pin numbering, same as the original grow-light
relay on pin 13).

Most cheap relay boards are Active-Low: pulling the pin LOW turns the relay
ON. Set `relay.active_low: false` in config.yaml if yours is the opposite.

When RPi.GPIO isn't installed (e.g. on a laptop) the relays are simulated and
just log what they would do, so you can test the schedule logic anywhere.
"""

import logging

try:
    import RPi.GPIO as GPIO
except ImportError:  # not on a Raspberry Pi
    GPIO = None

logger = logging.getLogger(__name__)


class Relay:
    def __init__(self, name: str, pin: int, active_low: bool = True, gpio=GPIO):
        self.name = name
        self.pin = int(pin)
        self.active_low = active_low
        self._gpio = gpio
        self._on = False

        if self._gpio is not None:
            self._gpio.setup(self.pin, self._gpio.OUT)
        self._write(False)  # always start OFF so nothing runs by accident on boot

    @property
    def simulated(self) -> bool:
        return self._gpio is None

    @property
    def is_on(self) -> bool:
        return self._on

    def set(self, on: bool):
        if on == self._on:
            return
        self._write(on)

    def _write(self, on: bool):
        self._on = on
        if self._gpio is None:
            logger.debug("[SIMULATED] %s (pin %d) -> %s", self.name, self.pin, "ON" if on else "OFF")
            return
        level_high = (not on) if self.active_low else on
        self._gpio.output(self.pin, self._gpio.HIGH if level_high else self._gpio.LOW)


def init_gpio(gpio=GPIO):
    if gpio is None:
        logger.warning("RPi.GPIO not available - relays are SIMULATED (nothing physical will switch)")
        return
    gpio.setmode(gpio.BOARD)
    gpio.setwarnings(False)


def cleanup_gpio(relays, gpio=GPIO):
    for relay in relays:
        try:
            relay.set(False)
        except Exception as e:
            logger.error("could not turn off %s: %s", relay.name, e)
    if gpio is not None:
        gpio.cleanup()
