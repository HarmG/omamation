"""The built-in blocks: triggers, conditions and actions.

Each block has a manifest: a description, its options as JSON Schema, its
outputs, and for actions `retry_safe` and a default timeout."""

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from . import paths
from .paths import write_atomic
from .system import System
from .values import seconds

DURATION_SCHEMA = {"type": "string", "pattern": r"^\d+(s|m|h)$"}


class StepFailed(Exception):
    """A step did not do what it should; the message is shown to the user."""


@dataclass
class Services:
    """What blocks may use. The daemon builds the real one, tests a fake one."""

    system: System
    microphone: "MicrophoneSource"
    cli: list[str]                                  # argv that starts this omamation
    notify_command: list[str] = field(default_factory=lambda: ["omarchy-notification-send"])
    meetings_dir: Path = Path.home() / "Meetings"
    export_poll: float = 3.0
    clock: Callable[[], datetime] = datetime.now     # local time, without a time zone
    schedule_tick: float = 30.0                      # seconds between schedule checks
    sources: dict = field(default_factory=dict)      # shared sources of integrations, by name


# --------------------------------------------------------------------------- triggers

Fire = Callable[[dict], Awaitable[None] | None]


class MicrophoneSource:
    """Which apps are recording from a microphone, kept up to date from PipeWire.

    One `pactl subscribe` serves every trigger. Monitor sources (what the
    speakers play) do not count as a microphone.
    """

    def __init__(self, system: System):
        self.system = system
        self.apps: set[str] | None = None
        self.error = ""
        self._listeners: list[Callable[[set[str]], None]] = []
        self._task: asyncio.Task | None = None

    def listen(self, callback: Callable[[set[str]], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        if self._task is None:
            self._task = asyncio.create_task(self._watch())
        elif self.apps is not None:
            callback(self.apps)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def refresh(self):
        outputs = await self.system.run(["pactl", "-f", "json", "list", "source-outputs"], timeout=5)
        sources = await self.system.run(["pactl", "-f", "json", "list", "sources", "short"], timeout=5)
        apps = parse_source_outputs(outputs.stdout, sources.stdout)
        if apps != self.apps:
            self.apps = apps
            for callback in list(self._listeners):
                callback(apps)

    async def _watch(self):
        delay = 1.0
        while True:
            try:
                await self.refresh()
                async for line in self.system.stream(["pactl", "subscribe"]):
                    if "source-output" in line:
                        await self.refresh()
                self.error = "pactl subscribe stopped; restarting"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reported in the bar, then retried
                self.error = f"microphone watch failed: {exc}"
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


def parse_source_outputs(outputs_json: str, sources_json: str) -> set[str]:
    try:
        outputs = json.loads(outputs_json or "[]")
        sources = json.loads(sources_json or "[]")
    except json.JSONDecodeError:
        return set()
    monitors = {s.get("index") for s in sources if str(s.get("name", "")).endswith(".monitor")}
    return {
        o.get("properties", {}).get("application.name", "")
        for o in outputs
        if o.get("source") not in monitors and o.get("properties", {}).get("application.name")
    }


def _matches(apps: set[str], prefix: str) -> str | None:
    # Return the first app whose name starts with the prefix ("Chromium" matches "Chromium input").
    for app in sorted(apps):
        if app.lower().startswith(prefix.lower()):
            return app
    return None


class Trigger:
    type = ""
    description = ""
    options: dict = {"type": "object", "additionalProperties": False}
    outputs: dict = {}

    def start(self, options: dict, services: Services, fire: Fire, key: str = "") -> Callable[[], None]:
        """Begin watching; returns a function that stops it. `key` is `<automation>/<step id>`."""
        return lambda: None


class _HeldMicrophone(Trigger):
    """Shared logic: fire once a condition on the microphone has held for `for`."""

    options = {
        "type": "object",
        "required": ["app"],
        "additionalProperties": False,
        "properties": {
            "app": {"type": "string", "minLength": 1, "description": "Start of the app's PipeWire name, e.g. Chromium"},
            "for": {**DURATION_SCHEMA, "description": "How long it must hold before the trigger fires"},
        },
    }
    outputs = {"app": "the matching app name"}
    want_present = True

    def start(self, options, services, fire, key=""):
        hold = seconds(options.get("for", "0s"))
        state = {"seen": False, "timer": None, "app": None}

        def update(apps: set[str]):
            app = _matches(apps, options["app"])
            if app:
                state["seen"], state["app"] = True, app
            # Start the hold timer when the wanted state begins; cancel it when the state ends.
            wanted = bool(app) if self.want_present else (not app and state["seen"])
            if wanted and state["timer"] is None:
                state["timer"] = asyncio.create_task(wait_and_fire())
            elif not wanted and state["timer"] is not None:
                state["timer"].cancel()
                state["timer"] = None

        async def wait_and_fire():
            await asyncio.sleep(hold)
            state["timer"] = None
            if not self.want_present:
                state["seen"] = False
            result = fire({"app": state["app"] or options["app"]})
            if asyncio.iscoroutine(result):
                await result

        unlisten = services.microphone.listen(update)

        def stop():
            unlisten()
            if state["timer"]:
                state["timer"].cancel()

        return stop


class MicrophoneInUse(_HeldMicrophone):
    type = "microphone_in_use"
    description = "An app has been using a microphone for a while"
    want_present = True


class MicrophoneReleased(_HeldMicrophone):
    type = "microphone_released"
    description = "An app that used a microphone has stopped using it for a while"
    want_present = False


DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SCHEDULE_GRACE = timedelta(minutes=2)


def _schedule_file() -> Path:
    return paths.state_dir() / "schedule.json"


def _last_fired(key: str) -> datetime | None:
    try:
        value = json.loads(_schedule_file().read_text()).get(key)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return datetime.fromisoformat(value) if value else None


def _set_last_fired(key: str, when: datetime):
    try:
        data = json.loads(_schedule_file().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data[key] = when.isoformat(timespec="seconds")
    write_atomic(_schedule_file(), json.dumps(data, indent=2, sort_keys=True) + "\n")


def latest_slot(options: dict, now: datetime) -> datetime | None:
    """The most recent `at` time on an allowed day at or before `now`, looking back a week."""
    days = {DAYS.index(d) for d in options.get("days", DAYS)}
    times = sorted(options["at"], reverse=True)
    for back in range(8):
        day = (now - timedelta(days=back)).date()
        if day.weekday() not in days:
            continue
        for at in times:
            hour, minute = map(int, at.split(":"))
            slot = datetime(day.year, day.month, day.day, hour, minute)
            if slot <= now:
                return slot
    return None


class Schedule(Trigger):
    type = "schedule"
    description = "At set times on set days, or every so often"
    options = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "days": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"enum": DAYS},
                     "description": "Days it may fire (default every day)"},
            "at": {"type": "array", "minItems": 1, "uniqueItems": True,
                   "items": {"type": "string", "pattern": r"^([01]\d|2[0-3]):[0-5]\d$"},
                   "description": "Times of day, HH:MM"},
            "every": {**DURATION_SCHEMA, "description": "Interval between runs"},
            "first_after": {**DURATION_SCHEMA, "description": "With every: first run this long after the engine starts (default: every)"},
            "missed": {"enum": ["skip", "catch_up"],
                       "description": "A time missed by more than 2 minutes (laptop asleep or off): skip it, or run once when back"},
        },
        "oneOf": [{"required": ["at"]}, {"required": ["every"]}],
        "dependentRequired": {"first_after": ["every"]},
    }
    outputs = {"slot": "the planned time (YYYY-MM-DD HH:MM)", "missed": "true when it runs late for a missed time"}

    def start(self, options, services, fire, key=""):
        catch_up = options.get("missed", "skip") == "catch_up"
        started = services.clock()
        # Without an earlier record, a time within the grace period still fires.
        if _last_fired(key) is None and "at" in options:
            _set_last_fired(key, started - SCHEDULE_GRACE)

        def due(now: datetime) -> tuple[datetime | None, datetime | None]:
            """The slot to handle now (or None), and what to record as last fired."""
            last = _last_fired(key)
            if "at" in options:
                slot = latest_slot(options, now)
                return (slot, slot) if slot and (last is None or slot > last) else (None, None)
            every = timedelta(seconds=seconds(options["every"]))
            slot = last + every if last else started + timedelta(seconds=seconds(options.get("first_after", options["every"])))
            if now < slot:
                return None, None
            if now - slot <= SCHEDULE_GRACE or catch_up:
                return slot, now
            # Skip: move to the last point of the interval grid that is not in the future.
            behind = (now - slot) // every
            return None, slot + behind * every

        async def tick():
            while True:
                now = services.clock()
                slot, record = due(now)
                if record is not None:
                    _set_last_fired(key, record)
                    late = slot is not None and now - slot > SCHEDULE_GRACE
                    if slot is not None and (not late or catch_up):
                        result = fire({"slot": slot.strftime("%Y-%m-%d %H:%M"), "missed": late})
                        if asyncio.iscoroutine(result):
                            await result
                await asyncio.sleep(services.schedule_tick)

        task = asyncio.create_task(tick())
        return task.cancel


class Manual(Trigger):
    type = "manual"
    description = "Started by hand: Run now in the bar, `omamation run`, or a notification button"


# --------------------------------------------------------------------------- conditions


class Condition:
    type = ""
    description = ""
    options: dict = {"type": "object", "additionalProperties": False}

    async def check(self, options: dict, services: Services, run_info: dict | None = None) -> tuple[bool, str]:
        raise NotImplementedError


class WindowOpen(Condition):
    type = "window_open"
    description = "A window of this class is open in Hyprland"
    options = {
        "type": "object",
        "required": ["class"],
        "additionalProperties": False,
        "properties": {"class": {"type": "string", "minLength": 1}},
    }

    async def check(self, options, services, run_info=None):
        result = await services.system.run(["hyprctl", "clients", "-j"], timeout=5)
        if result.code != 0:
            raise StepFailed(f"hyprctl failed: {result.stderr.strip() or result.code}")
        classes = {c.get("class") for c in json.loads(result.stdout or "[]")}
        found = options["class"] in classes
        return found, f"window {options['class']} {'open' if found else 'not open'}"


async def voxtype_meeting_state(services: Services) -> str:
    result = await services.system.run(["voxtype", "meeting", "status"], timeout=10)
    if re.search(r"recording|paused", result.stdout, re.IGNORECASE):
        return "recording"
    if result.code != 0 and "no meeting" not in result.stdout.lower():
        raise StepFailed(f"voxtype meeting status failed: {result.stderr.strip() or result.code}")
    return "idle"


class VoxtypeMeeting(Condition):
    type = "voxtype_meeting"
    description = "Voxtype is (not) recording a meeting"
    options = {
        "type": "object",
        "required": ["state"],
        "additionalProperties": False,
        "properties": {"state": {"enum": ["idle", "recording"]}},
    }

    async def check(self, options, services, run_info=None):
        state = await voxtype_meeting_state(services)
        return state == options["state"], f"voxtype meeting is {state}"


class NotRanToday(Condition):
    type = "not_ran_today"
    description = "This automation has not had a successful run today"

    async def check(self, options, services, run_info=None):
        if not run_info:
            raise StepFailed("not_ran_today needs the automation it belongs to")
        today = services.clock().date()
        folder = paths.traces_dir() / run_info["automation"]
        for path in sorted(folder.glob("*.json"), reverse=True) if folder.exists() else []:
            try:
                trace = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if trace.get("dry_run") or trace.get("result") != "success":
                continue
            if datetime.fromisoformat(trace["started"]).date() == today:
                return False, f"already ran successfully today ({trace['run_id']})"
        return True, "no successful run today"


# --------------------------------------------------------------------------- actions


class Action:
    type = ""
    description = ""
    options: dict = {"type": "object", "additionalProperties": False}
    outputs: dict = {}
    retry_safe = True
    default_timeout = "60s"

    async def run(self, options: dict, services: Services, run_info: dict | None = None) -> dict:
        raise NotImplementedError


class RunCommand(Action):
    type = "run_command"
    description = "Run a program; a non-zero exit code is an error"
    options = {
        "type": "object",
        "required": ["command"],
        "additionalProperties": False,
        "properties": {"command": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
    }
    outputs = {"exit_code": "exit code", "stdout": "standard output", "stderr": "error output"}
    retry_safe = False

    async def run(self, options, services, run_info=None):
        result = await services.system.run([str(part) for part in options["command"]])
        output = {"exit_code": result.code, "stdout": result.stdout, "stderr": result.stderr}
        if result.code != 0:
            raise StepFailed(f"{options['command'][0]} exited with {result.code}: {result.stderr.strip()[:300]}")
        return output


BUTTON_SCHEMA = {
    "type": "object",
    "required": ["label"],
    "additionalProperties": False,
    "properties": {
        "label": {"type": "string", "minLength": 1},
        "run": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9-]*$", "description": "Automation to start"},
        "trace": {"type": "boolean", "description": "Open the trace of this run"},
    },
    "oneOf": [{"required": ["run"]}, {"required": ["trace"]}],
}


class Notify(Action):
    type = "notify"
    description = "Show a desktop notification, with up to three buttons"
    options = {
        "type": "object",
        "required": ["title"],
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "body": {"type": "string"},
            "urgency": {"enum": ["low", "normal", "critical"]},
            "buttons": {"type": "array", "maxItems": 3, "items": BUTTON_SCHEMA},
        },
    }
    retry_safe = False
    default_timeout = "10s"

    async def run(self, options, services, run_info=None):
        argv = [*services.notify_command, "--app-name", "Omamation",
                "-u", options.get("urgency", "normal"), options["title"]]
        if options.get("body"):
            argv.append(options["body"])
        for button in options.get("buttons", []):
            if button.get("run"):
                argv += ["--action", button["label"], *services.cli, "run", button["run"]]
            elif button.get("trace") and run_info:
                argv += ["--action", button["label"], "omarchy-launch-floating-terminal-with-presentation",
                         *services.cli, "trace", run_info["automation"], "--run", run_info["run_id"]]
        result = await services.system.run(argv, timeout=10)
        if result.code != 0:
            raise StepFailed(f"notification failed: {result.stderr.strip() or result.code}")
        return {}


class VoxtypeMeetingStart(Action):
    type = "voxtype_meeting_start"
    description = "Start a Voxtype meeting recording"
    options = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"title": {"type": "string"}},
    }
    outputs = {"title": "title of the recording"}

    async def run(self, options, services, run_info=None):
        # Return without starting when a recording is already running.
        if await voxtype_meeting_state(services) == "recording":
            return {"title": options.get("title", ""), "already_recording": True}
        title = options.get("title") or f"Meeting {datetime.now():%Y-%m-%d %H:%M}"
        result = await services.system.run(["voxtype", "meeting", "start", "-t", title], timeout=20)
        if result.code != 0:
            raise StepFailed(f"voxtype meeting start failed: {result.stderr.strip() or result.code}")
        return {"title": title, "already_recording": False}


class VoxtypeMeetingStop(Action):
    type = "voxtype_meeting_stop"
    description = "Stop the Voxtype meeting recording and save the transcript as Markdown"
    options = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"folder": {"type": "string", "description": "Where the transcript goes (default ~/Meetings)"}},
    }
    outputs = {"file": "path of the saved transcript"}
    retry_safe = False
    default_timeout = "150s"

    async def run(self, options, services, run_info=None):
        if await voxtype_meeting_state(services) != "recording":
            raise StepFailed("no Voxtype meeting is recording")
        result = await services.system.run(["voxtype", "meeting", "stop"], timeout=30)
        if result.code != 0:
            raise StepFailed(f"voxtype meeting stop failed: {result.stderr.strip() or result.code}")
        folder = Path(options["folder"]).expanduser() if options.get("folder") else services.meetings_dir
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{datetime.now():%Y-%m-%d_%H%M}.md"
        # Export the latest meeting every few seconds until the file contains words.
        while True:
            await asyncio.sleep(services.export_poll)
            export = await services.system.run(
                ["voxtype", "meeting", "export", "latest", "--speakers", "--timestamps", "--metadata", "-o", str(target)],
                timeout=60,
            )
            if export.code == 0 and target.exists() and "Word Count:** 0\n" not in target.read_text(errors="replace") + "\n":
                return {"file": str(target)}


# --------------------------------------------------------------------------- registry

TRIGGERS = {cls.type: cls() for cls in (MicrophoneInUse, MicrophoneReleased, Schedule, Manual)}
CONDITIONS = {cls.type: cls() for cls in (WindowOpen, VoxtypeMeeting, NotRanToday)}
ACTIONS = {cls.type: cls() for cls in (RunCommand, Notify, VoxtypeMeetingStart, VoxtypeMeetingStop)}

from .integrations import register  # noqa: E402 - integrations build on the classes above

register(TRIGGERS, CONDITIONS, ACTIONS)


def catalogue() -> dict:
    """Every block with its manifest, for `omamation blocks` and agents."""

    def describe(block, kind):
        entry = {"type": block.type, "kind": kind, "description": block.description, "options": block.options}
        if getattr(block, "integration", None):
            entry["integration"] = block.integration
        if getattr(block, "outputs", None):
            entry["outputs"] = block.outputs
        if kind == "action":
            entry["retry_safe"] = block.retry_safe
            entry["default_timeout"] = block.default_timeout
        return entry

    return {
        "triggers": [describe(b, "trigger") for b in TRIGGERS.values()],
        "conditions": [describe(b, "condition") for b in CONDITIONS.values()],
        "actions": [describe(b, "action") for b in ACTIONS.values()],
    }
