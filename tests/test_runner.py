import asyncio
import json

from omamation import model, paths, runner
from omamation.system import Result

TRIGGER = {"id": "mic", "type": "microphone_in_use", "time": "2026-11-17 10:00", "app": "Chromium input"}


def run(doc, services, **kwargs):
    return asyncio.run(runner.execute(model.with_defaults(doc), TRIGGER, services, **kwargs))


def notifications(system):
    return [c for c in system.calls if c[0] == "notify"]


def test_start_records_and_notifies(dirs, services, system, start_doc):
    trace = run(start_doc, services)
    assert trace["result"] == "success"
    assert ["voxtype", "meeting", "start", "-t", "Teams 2026-11-17 10:00"] in system.calls
    note = notifications(system)[0]
    assert "Recording started" in note and "Teams 2026-11-17 10:00" in note
    # The Stop button starts the stop automation through the CLI.
    assert note[note.index("--action"):][:5] == ["--action", "Stop", "omamation", "run", "teams-recording-stop"]
    saved = json.loads((paths.traces_dir() / "teams-recording-start" / f"{trace['run_id']}.json").read_text())
    assert [c["passed"] for c in saved["conditions"]] == [True, True]


def test_no_teams_window_means_skipped(dirs, services, system, start_doc):
    system.windows = ["firefox"]
    trace = run(start_doc, services)
    assert trace["result"] == "skipped"
    assert not any(c[:3] == ["voxtype", "meeting", "start"] for c in system.calls)
    assert notifications(system) == []


def test_already_recording_means_skipped(dirs, services, system, start_doc):
    system.voxtype = "recording"
    assert run(start_doc, services)["result"] == "skipped"


def test_failure_runs_the_default_catch(dirs, services, system, start_doc):
    system.on("voxtype", "meeting", "start", result=Result(1, "", "no microphone"))
    start_doc["actions"][0]["retry_delay"] = "0s"
    trace = run(start_doc, services)
    assert trace["result"] == "failed"
    assert trace["error"] == {"step": "record", "message": "voxtype meeting start failed: no microphone"}
    assert trace["steps"][0]["attempts"] == 2          # retried once: the action is retry_safe
    note = notifications(system)[0]
    assert "Teams recording start failed" in note and "record: voxtype meeting start failed: no microphone" in note
    button = note[note.index("--action"):]
    assert button == ["--action", "Trace", "omarchy-launch-floating-terminal-with-presentation",
                      "omamation", "trace", "teams-recording-start", "--run", trace["run_id"]]


def test_retry_succeeds_the_second_time(dirs, services, system, start_doc):
    attempts = []

    def flaky(argv):
        attempts.append(argv)
        if len(attempts) == 1:
            return Result(1, "", "busy")
        system.voxtype = "recording"
        return Result(0, "", "")

    system.on("voxtype", "meeting", "start", func=flaky)
    start_doc["actions"][0]["retry_delay"] = "0s"
    assert run(start_doc, services)["result"] == "success"
    assert len(attempts) == 2


def test_timeout_is_an_error(dirs, services, system, start_doc):
    async def hang(argv):
        await asyncio.sleep(10)

    system.on("voxtype", "meeting", "start", func=hang)
    start_doc["actions"][0].update(timeout="1s", retry=0)
    trace = run(start_doc, services)
    assert trace["result"] == "failed"
    assert trace["error"]["message"] == "timed out after 1s"


def test_own_catch_replaces_the_default(dirs, services, system, start_doc):
    system.on("voxtype", "meeting", "start", result=Result(1, "", "boom"))
    start_doc["actions"][0]["retry"] = 0
    start_doc["on_error"] = [{"id": "log", "action": "run_command",
                              "with": {"command": ["logger", "{{ error.step }} {{ error.message }}"]}}]
    trace = run(start_doc, services)
    assert ["logger", "record voxtype meeting start failed: boom"] in system.calls
    assert notifications(system) == []
    assert trace["catch"][0]["status"] == "success"


def test_failing_catch_is_kept_in_the_trace(dirs, services, system, start_doc):
    system.on("voxtype", "meeting", "start", result=Result(1, "", "boom"))
    system.on("notify", result=Result(1, "", "no notification daemon"))
    start_doc["actions"][0]["retry"] = 0
    trace = run(start_doc, services)
    assert trace["catch_error"]["step"] == "notify_failure"


def test_condition_that_cannot_be_checked_is_a_failure(dirs, services, system, start_doc):
    system.on("hyprctl", result=Result(1, "", "Hyprland is not running"))
    trace = run(start_doc, services)
    assert trace["result"] == "failed"
    assert trace["error"]["step"] == "teams_open"
    assert len(notifications(system)) == 1


def test_stop_waits_for_the_transcript(dirs, services, system, stop_doc, tmp_path):
    system.voxtype = "recording"
    system.export_empty_times = 2
    stop_doc["actions"][0]["with"]["folder"] = str(tmp_path / "out")
    trace = run(stop_doc, services)
    assert trace["result"] == "success"
    assert system.export_attempts == 3
    saved = trace["steps"][0]["output"]["file"]
    assert saved.startswith(str(tmp_path / "out")) and "Word Count:** 42" in open(saved).read()
    assert saved in notifications(system)[0]


def test_dry_run_changes_nothing(dirs, services, system, start_doc):
    trace = run(start_doc, services, dry_run=True)
    assert trace["result"] == "success" and trace["dry_run"]
    assert [s["status"] for s in trace["steps"]] == ["dry_run", "dry_run"]
    assert not any(c[:3] == ["voxtype", "meeting", "start"] for c in system.calls)
    assert notifications(system) == []


def test_secrets_are_masked():
    assert runner.mask({"api_token": "abc", "nested": [{"password": "x", "ok": 1}]}) == \
        {"api_token": "***", "nested": [{"password": "***", "ok": 1}]}
