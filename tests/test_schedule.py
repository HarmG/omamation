"""The schedule trigger and the not_ran_today condition, with a fake clock."""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from omamation import paths
from omamation.blocks import CONDITIONS, TRIGGERS, _set_last_fired
from omamation.model import validate

MONDAY = datetime(2026, 11, 16)
KEY = "test/when"


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock(services):
    services.clock = Clock(MONDAY.replace(hour=6, minute=59))
    services.schedule_tick = 0.01
    return services.clock


async def collect(services, options, steps):
    """Start the trigger, run each step (a time to set) and return what fired."""
    fired = []
    stop = TRIGGERS["schedule"].start(options, services, fired.append, key=KEY)
    try:
        for when in steps:
            services.clock.now = when
            await asyncio.sleep(0.05)
    finally:
        stop()
    return fired


def at(hour, minute=0, second=0, day=MONDAY):
    return day.replace(hour=hour, minute=minute, second=second)


def test_fires_once_at_the_time(dirs, services, clock):
    options = {"days": ["mon", "tue", "wed", "thu"], "at": ["07:00", "08:00"]}
    fired = asyncio.run(collect(services, options, [at(6, 59, 50), at(7, 0, 10), at(7, 0, 40), at(7, 30)]))
    assert fired == [{"slot": "2026-11-16 07:00", "missed": False}]


def test_not_on_other_days(dirs, services, clock):
    friday = MONDAY + timedelta(days=4)
    clock.now = at(6, 59, day=friday)
    fired = asyncio.run(collect(services, {"days": ["mon", "tue", "wed", "thu"], "at": ["07:00"]},
                                [at(7, 0, 10, day=friday)]))
    assert fired == []


def test_no_double_fire_after_restart(dirs, services, clock):
    options = {"at": ["07:00"]}
    first = asyncio.run(collect(services, options, [at(7, 0, 10)]))
    clock.now = at(7, 0, 40)
    second = asyncio.run(collect(services, options, [at(7, 1)]))
    assert len(first) == 1 and second == []


@pytest.mark.parametrize("missed, expected", [
    ("skip", []),
    ("catch_up", [{"slot": "2026-11-16 09:00", "missed": True}]),
])
def test_missed_after_sleep(dirs, services, clock, missed, expected):
    _set_last_fired(KEY, at(7))
    clock.now = at(9, 30)
    fired = asyncio.run(collect(services, {"at": ["07:00", "08:00", "09:00"], "missed": missed},
                                [at(9, 30), at(9, 31)]))
    assert fired == expected


def test_every_with_first_after(dirs, services, clock):
    clock.now = at(10)
    options = {"every": "1h", "first_after": "5m"}
    fired = asyncio.run(collect(services, options, [at(10, 4), at(10, 5), at(10, 30), at(11, 4), at(11, 5, 5)]))
    assert [f["missed"] for f in fired] == [False, False]
    assert [f["slot"] for f in fired] == ["2026-11-16 10:05", "2026-11-16 11:05"]


def test_every_skips_a_gap(dirs, services, clock):
    clock.now = at(10)
    fired = asyncio.run(collect(services, {"every": "1h", "first_after": "5m"},
                                [at(10, 5), at(13, 30), at(14, 5, 10)]))
    assert [f["slot"] for f in fired] == ["2026-11-16 10:05", "2026-11-16 14:05"]


def test_schedule_options_are_checked(start_doc):
    for options, ok in [({"at": ["07:00"]}, True), ({"every": "1h", "first_after": "5m"}, True),
                        ({"at": ["07:00"], "every": "1h"}, False), ({"at": ["7:00"]}, False),
                        ({"at": ["07:00"], "first_after": "5m"}, False), ({"days": ["mon"]}, False)]:
        start_doc["triggers"] = [{"id": "when", "trigger": "schedule", "with": options}]
        assert (validate(start_doc) == []) is ok, options


def write_trace(run_id, started, result, dry_run=False):
    folder = paths.traces_dir() / "brief"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{run_id}.json").write_text(json.dumps(
        {"run_id": run_id, "started": started.astimezone().isoformat(), "result": result, "dry_run": dry_run}))


@pytest.mark.parametrize("trace, expected", [
    (None, True),
    (("a", at(7), "success", False), False),
    (("a", at(7), "failed", False), True),
    (("a", at(7), "success", True), True),
    (("a", at(7) - timedelta(days=1), "success", False), True),
])
def test_not_ran_today(dirs, services, clock, trace, expected):
    clock.now = at(9)
    if trace:
        write_trace(*trace)
    passed, _ = asyncio.run(CONDITIONS["not_ran_today"].check({}, services, {"automation": "brief", "run_id": "x"}))
    assert passed is expected
