"""Integration m365 with a fake Graph and a fake clock."""

import asyncio
from datetime import datetime, timedelta

import pytest

from omamation.blocks import ACTIONS, TRIGGERS
from omamation.integrations import m365
from omamation.integrations.m365 import CalendarSource, IntegrationError, to_event

NOW = datetime(2026, 11, 18, 9, 0)
KEY = "prep/when"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def event(subject="Daily support", start=NOW + timedelta(minutes=40), minutes=15, **extra):
    base = {"event_id": subject.lower().replace(" ", "-"), "subject": subject, "start": start,
            "end": start + timedelta(minutes=minutes), "online": True, "others": 3, "show_as": "busy",
            "cancelled": False, "all_day": False, "join_url": "https://teams/x", "location": ""}
    return {**base, **extra}


class FakeGraph:
    def __init__(self, events):
        self.events = events
        self.calls = 0
        self.fail = None

    def __call__(self, start, end):
        self.calls += 1
        if self.fail:
            raise IntegrationError(self.fail)
        return list(self.events)


@pytest.fixture
def calendar(services):
    services.clock = Clock(NOW)
    services.schedule_tick = 0.01
    graph = FakeGraph([event()])
    services.sources["m365"] = CalendarSource(fetch=graph, clock=services.clock, poll=0.02)
    return graph


async def collect(services, options, steps):
    fired = []
    stop = TRIGGERS["calendar_event"].start(options, services, fired.append, key=KEY)
    try:
        await asyncio.sleep(0.05)
        for when in steps:
            services.clock.now = when
            await asyncio.sleep(0.05)
    finally:
        stop()
    return fired


def test_graph_item_is_read():
    item = {"id": "A", "subject": "Daily support", "start": {"dateTime": "2026-11-18T08:30:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-11-18T08:45:00.0000000", "timeZone": "UTC"}, "isOnlineMeeting": True,
            "showAs": "busy", "organizer": {"emailAddress": {"address": "boss@example.com"}},
            "attendees": [{"emailAddress": {"address": "Me@example.com"}}]}
    e = to_event(item, "me@example.com")
    assert e["others"] == 1 and e["online"] and e["subject"] == "Daily support"
    assert e["start"] == datetime(2026, 11, 18, 8, 30) + (datetime.now().astimezone().utcoffset() or timedelta())


def test_fires_once_before_the_start(dirs, services, calendar):
    options = {"before": "30m", "subject": "daily support", "online": True, "with_others": True}
    fired = asyncio.run(collect(services, options, [NOW + timedelta(minutes=9), NOW + timedelta(minutes=10),
                                                   NOW + timedelta(minutes=11)]))
    assert [f["subject"] for f in fired] == ["Daily support"]
    assert fired[0]["start"] == "2026-11-18 09:40"


def test_filters(dirs, services, calendar):
    calendar.events = [event("Daily support", show_as="free"), event("Daily support x", cancelled=True),
                       event("Daily support y", others=0), event("Daily support z", online=False),
                       event("Lunch"), event("Daily support all", all_day=True)]
    options = {"before": "1h", "subject": "daily support", "online": True, "with_others": True}
    assert asyncio.run(collect(services, options, [NOW])) == []


def test_late_start_fires_until_the_meeting_begins(dirs, services, calendar):
    calendar.events = [event(start=NOW + timedelta(minutes=5)), event("Daily support old", start=NOW - timedelta(minutes=1))]
    fired = asyncio.run(collect(services, {"before": "30m"}, [NOW]))
    assert [f["subject"] for f in fired] == ["Daily support"]


def test_no_double_fire_after_restart_but_again_when_moved(dirs, services, calendar):
    options = {"before": "30m"}
    first = asyncio.run(collect(services, options, [NOW + timedelta(minutes=10)]))
    again = asyncio.run(collect(services, options, [NOW + timedelta(minutes=11)]))
    calendar.events = [event(start=NOW + timedelta(minutes=50))]
    moved = asyncio.run(collect(services, options, [NOW + timedelta(minutes=21)]))
    assert len(first) == 1 and again == [] and moved[0]["start"] == "2026-11-18 09:50"


def test_error_keeps_the_last_calendar(dirs, services, calendar):
    async def scenario():
        source = services.sources["m365"]
        stop = TRIGGERS["calendar_event"].start({"before": "30m"}, services, fired.append, key=KEY)
        await asyncio.sleep(0.05)
        calendar.fail = "m365: Graph not reachable"
        await asyncio.sleep(0.05)
        assert source.error == "m365: Graph not reachable" and source.events
        services.clock.now = NOW + timedelta(minutes=10)
        await asyncio.sleep(0.05)
        stop()

    fired = []
    asyncio.run(scenario())
    assert len(fired) == 1


@pytest.mark.parametrize("now, fail, expected", [
    (NOW + timedelta(minutes=45), None, ("Daily support", True)),
    (NOW + timedelta(minutes=36), None, ("Daily support", True)),
    (NOW, None, ("Teams 09:00", False)),
    (NOW + timedelta(minutes=45), "m365: no token", ("Teams 09:00", False)),
])
def test_calendar_now(dirs, services, calendar, now, fail, expected):
    services.clock.now = now
    calendar.fail = fail
    out = asyncio.run(ACTIONS["calendar_now"].run({"fallback": "Teams 09:00", "online": True}, services))
    assert (out["subject"], out["found"]) == expected
    assert bool(out["error"]) == bool(fail)


def test_missing_settings(dirs, monkeypatch, tmp_path):
    monkeypatch.setenv("OMAMATION_CONFIG_DIR", str(tmp_path / "config"))
    with pytest.raises(IntegrationError, match="not set up"):
        m365.settings()
