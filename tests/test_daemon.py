"""The engine end to end: triggers from (fake) PipeWire, runs, traces and the widget's state file."""

import asyncio
import json

from omamation import paths
from omamation.blocks import parse_source_outputs
from omamation.daemon import Daemon
from omamation.store import Store

HOLD = {"for": "1s"}


def approved_store(*docs):
    store = Store()
    for doc in docs:
        store.save(doc, by="test")
        store.approve(doc["id"], store.request_approval(doc["id"]))
    return store


def short_holds(start_doc, stop_doc):
    start_doc["triggers"][0]["with"].update(HOLD)
    stop_doc["triggers"][0]["with"].update(HOLD)


def state():
    return json.loads(paths.state_file().read_text())


def test_monitor_sources_do_not_count():
    outputs = json.dumps([{"source": 1, "properties": {"application.name": "Chromium input"}},
                          {"source": 2, "properties": {"application.name": "voxtype"}}])
    sources = json.dumps([{"index": 1, "name": "alsa_input"}, {"index": 2, "name": "alsa_output.monitor"}])
    assert parse_source_outputs(outputs, sources) == {"Chromium input"}


def test_meeting_starts_and_stops(dirs, services, system, start_doc, stop_doc):
    short_holds(start_doc, stop_doc)
    store = approved_store(start_doc, stop_doc)

    async def scenario():
        daemon = Daemon(services, store)
        daemon.reload()
        await asyncio.sleep(0.1)
        await system.set_mic(["Chromium input"])
        await asyncio.sleep(0.5)
        assert system.voxtype == "idle"            # not yet: the trigger waits 1s
        await asyncio.sleep(1.0)
        assert system.voxtype == "recording"
        await system.set_mic([])
        await asyncio.sleep(1.5)
        assert system.voxtype == "idle"
        for _ in range(50):                        # let the export loop finish
            if not daemon.running:
                break
            await asyncio.sleep(0.05)
        for stop in daemon.stops:
            stop()
        await services.microphone.stop()

    asyncio.run(scenario())
    by_id = {a["id"]: a for a in state()["automations"]}
    assert by_id["teams-recording-start"]["last_result"]["result"] == "success"
    assert by_id["teams-recording-stop"]["last_result"]["result"] == "success"
    assert state()["attention"] is False


def test_short_mic_use_does_not_record(dirs, services, system, start_doc, stop_doc):
    short_holds(start_doc, stop_doc)
    store = approved_store(start_doc, stop_doc)

    async def scenario():
        daemon = Daemon(services, store)
        daemon.reload()
        await asyncio.sleep(0.1)
        await system.set_mic(["Chromium input"])
        await asyncio.sleep(0.4)
        await system.set_mic([])
        await asyncio.sleep(1.2)
        await services.microphone.stop()

    asyncio.run(scenario())
    assert system.voxtype == "idle"
    assert not any(c[:3] == ["voxtype", "meeting", "start"] for c in system.calls)


def test_concepts_do_not_run(dirs, services, system, start_doc):
    start_doc["triggers"][0]["with"].update(HOLD)
    store = Store()
    store.save(start_doc, by="agent")

    async def scenario():
        daemon = Daemon(services, store)
        daemon.reload()
        assert daemon.stops == []
        await services.microphone.stop()

    asyncio.run(scenario())
    assert state()["automations"][0]["status"] == "concept"


def test_single_mode_ignores_a_second_trigger(dirs, services, system, start_doc):
    store = approved_store(start_doc)

    async def slow(argv):
        await asyncio.sleep(0.3)
        system.voxtype = "recording"
        from omamation.system import Result
        return Result(0, "", "")

    system.on("voxtype", "meeting", "start", func=slow)

    async def scenario():
        daemon = Daemon(services, store)
        daemon.reload()
        assert daemon.start_run("teams-recording-start", {"id": "manual", "type": "manual"}) == "started"
        assert daemon.start_run("teams-recording-start", {"id": "manual", "type": "manual"}) == "already running"
        await asyncio.sleep(0.6)
        await services.microphone.stop()

    asyncio.run(scenario())
    assert sum(c[:3] == ["voxtype", "meeting", "start"] for c in system.calls) == 1


def test_failed_run_asks_for_attention(dirs, services, system, start_doc):
    from omamation.system import Result

    system.on("voxtype", "meeting", "start", result=Result(1, "", "boom"))
    start_doc["actions"][0].update(retry=0)
    store = approved_store(start_doc)

    async def scenario():
        daemon = Daemon(services, store)
        daemon.reload()
        daemon.start_run("teams-recording-start", {"id": "manual", "type": "manual"})
        await asyncio.sleep(0.2)
        await services.microphone.stop()

    asyncio.run(scenario())
    assert state()["attention"] is True
    assert state()["automations"][0]["last_result"]["error"]["step"] == "record"
