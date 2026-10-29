from datetime import datetime

import pytest

from src.schedules import Daily, Interval, Times, build_schedule


def at(hhmmss: str) -> datetime:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return datetime(2026, 11, 1, h, m, s)


def test_daily_window():
    light = Daily("06:00", "22:00")
    assert not light.is_on(at("05:59:59"))
    assert light.is_on(at("06:00:00"))
    assert light.is_on(at("21:59:59"))
    assert not light.is_on(at("22:00:00"))
    assert light.seconds_until_change(at("21:00:00")) == 3600
    assert light.seconds_until_change(at("23:00:00")) == 7 * 3600


def test_daily_across_midnight():
    light = Daily("20:00", "04:00")
    assert light.is_on(at("23:00:00"))
    assert light.is_on(at("03:59:59"))
    assert not light.is_on(at("12:00:00"))


def test_interval_pump_every_6h_from_6am():
    pump = Interval(every_minutes=360, run_seconds=5, start_at="06:00")
    for slot in ("00:00", "06:00", "12:00", "18:00"):
        assert pump.is_on(at(slot + ":00"))
        assert pump.is_on(at(slot + ":04"))
        assert not pump.is_on(at(slot + ":05"))
    assert not pump.is_on(at("09:00:00"))
    assert pump.seconds_until_change(at("06:00:02")) == 3
    assert pump.seconds_until_change(at("11:00:00")) == 3600


def test_interval_same_slots_every_day():
    pump = Interval(every_minutes=360, run_seconds=5, start_at="06:00")
    assert pump.is_on(datetime(2026, 11, 1, 6, 0, 1))
    assert pump.is_on(datetime(2027, 3, 15, 6, 0, 1))


def test_interval_fan_10_min_every_30():
    fan = Interval(every_minutes=30, run_seconds=600)
    assert fan.is_on(at("10:00:00"))
    assert fan.is_on(at("10:09:59"))
    assert not fan.is_on(at("10:10:00"))
    assert fan.is_on(at("10:30:00"))


def test_times():
    pump = Times(at=["19:00", "07:00"], run_seconds=10)
    assert pump.is_on(at("07:00:09"))
    assert not pump.is_on(at("07:00:10"))
    assert pump.is_on(at("19:00:00"))
    assert pump.seconds_until_change(at("08:00:00")) == 11 * 3600


@pytest.mark.parametrize("cfg", [
    {"type": "interval", "every_minutes": 1, "run_seconds": 60},   # run >= interval
    {"type": "daily", "on_time": "06:00", "off_time": "06:00"},
    {"type": "daily", "on_time": "25:00", "off_time": "06:00"},
    {"type": "times", "at": [], "run_seconds": 5},
    {"type": "interval", "every_minutes": 10},                     # missing run_seconds
    {"type": "sometimes"},
])
def test_bad_configs_are_rejected(cfg):
    with pytest.raises(ValueError):
        build_schedule(cfg)
