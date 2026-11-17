"""The built-in blocks: triggers, conditions and actions.

Every block has a manifest: what it does, the options it takes (as JSON
Schema), what it returns, and whether it is safe to run twice (`retry_safe`).
The engine validates each step against its block's options before it saves
an automation, so the editor and agents can only build what a block accepts.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

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
    # Browsers name their stream after themselves plus a suffix ("Chromium input").
    for app in sorted(apps):
        if app.lower().startswith(prefix.lower()):
            return app
    return None


class Trigger:
    type = ""
    description = ""
    options: dict = {"type": "object", "additionalProperties": False}
    outputs: dict = {}

    def start(self, options: dict, services: Services, fire: Fire) -> Callable[[], None]:
        """Begin watching; returns a function that stops it."""
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

    def start(self, options, services, fire):
        hold = seconds(options.get("for", "0s"))
        state = {"seen": False, "timer": None, "app": None}

        def update(apps: set[str]):
            app = _matches(apps, options["app"])
            if app:
                state["seen"], state["app"] = True, app
            # "released" only means something after the app had the microphone.
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


class Manual(Trigger):
    type = "manual"
    description = "Started by hand: Run now in the bar, `omamation run`, or a notification button"


# --------------------------------------------------------------------------- conditions


class Condition:
    type = ""
    description = ""
    options: dict = {"type": "object", "additionalProperties": False}

    async def check(self, options: dict, services: Services) -> tuple[bool, str]:
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

    async def check(self, options, services):
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

    async def check(self, options, services):
        state = await voxtype_meeting_state(services)
        return state == options["state"], f"voxtype meeting is {state}"


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
    # A command can do anything, so running it twice is not assumed to be safe.
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
        # Safe to repeat: a recording that is already running counts as started.
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
    # A second stop has nothing to stop, so it is not retried by default.
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
        # Voxtype still transcribes the last chunks after a stop; the export is
        # empty until it has, so try until there are words (the step timeout ends it).
        while True:
            await asyncio.sleep(services.export_poll)
            export = await services.system.run(
                ["voxtype", "meeting", "export", "latest", "--speakers", "--timestamps", "--metadata", "-o", str(target)],
                timeout=60,
            )
            if export.code == 0 and target.exists() and "Word Count:** 0\n" not in target.read_text(errors="replace") + "\n":
                return {"file": str(target)}


# --------------------------------------------------------------------------- registry

TRIGGERS = {cls.type: cls() for cls in (MicrophoneInUse, MicrophoneReleased, Manual)}
CONDITIONS = {cls.type: cls() for cls in (WindowOpen, VoxtypeMeeting)}
ACTIONS = {cls.type: cls() for cls in (RunCommand, Notify, VoxtypeMeetingStart, VoxtypeMeetingStop)}


def catalogue() -> dict:
    """Every block with its manifest, for `omamation blocks` and agents."""

    def describe(block, kind):
        entry = {"type": block.type, "kind": kind, "description": block.description, "options": block.options}
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
