import asyncio
import json
from pathlib import Path

import pytest

from omamation.blocks import MicrophoneSource, Services
from omamation.system import Result


class FakeSystem:
    """Answers commands from a table and records every call; nothing real runs."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.handlers: dict[tuple, object] = {}
        self.lines: asyncio.Queue | None = None
        self.mic_apps: list[str] = []
        self.voxtype = "idle"
        self.windows = ["chrome-teams.cloud.microsoft__-Default"]
        self.export_attempts = 0
        self.export_empty_times = 1

    def on(self, *prefix, result=None, func=None):
        self.handlers[prefix] = func or (lambda argv: result)

    async def run(self, argv, timeout=None):
        self.calls.append(list(argv))
        for prefix in sorted(self.handlers, key=len, reverse=True):
            if tuple(argv[: len(prefix)]) == prefix:
                answer = self.handlers[prefix](argv)
                if asyncio.iscoroutine(answer):
                    answer = await answer
                return answer
        return self.default(argv)

    def default(self, argv):
        if argv[:4] == ["pactl", "-f", "json", "list"]:
            if argv[4] == "source-outputs":
                return Result(0, json.dumps([{"source": 1, "properties": {"application.name": a}}
                                             for a in self.mic_apps]), "")
            return Result(0, json.dumps([{"index": 1, "name": "alsa_input.mic"},
                                         {"index": 2, "name": "alsa_output.monitor"}]), "")
        if argv[:2] == ["hyprctl", "clients"]:
            return Result(0, json.dumps([{"class": c} for c in self.windows]), "")
        if argv[:3] == ["voxtype", "meeting", "status"]:
            if self.voxtype == "recording":
                return Result(0, "Meeting: recording\n", "")
            return Result(0, "No meeting currently in progress.\n", "")
        if argv[:3] == ["voxtype", "meeting", "start"]:
            self.voxtype = "recording"
            return Result(0, "", "")
        if argv[:3] == ["voxtype", "meeting", "stop"]:
            self.voxtype = "idle"
            return Result(0, "", "")
        if argv[:3] == ["voxtype", "meeting", "export"]:
            self.export_attempts += 1
            target = Path(argv[argv.index("-o") + 1])
            words = 0 if self.export_attempts <= self.export_empty_times else 42
            target.write_text(f"# Meeting\n- **Word Count:** {words}\n")
            return Result(0, "", "")
        return Result(0, "", "")

    async def stream(self, argv):
        self.calls.append(list(argv))
        self.lines = asyncio.Queue()
        while True:
            line = await self.lines.get()
            if line is None:
                return
            yield line

    async def set_mic(self, apps):
        """Change who uses the microphone and let PipeWire announce it."""
        self.mic_apps = apps
        while self.lines is None:
            await asyncio.sleep(0.01)
        await self.lines.put("Event 'new' on source-output #1\n")
        await asyncio.sleep(0.05)


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAMATION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OMAMATION_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMAMATION_RUNTIME_DIR", str(tmp_path / "run"))
    return tmp_path


@pytest.fixture
def system():
    return FakeSystem()


@pytest.fixture
def services(system, tmp_path):
    return Services(system=system, microphone=MicrophoneSource(system), cli=["omamation"],
                    notify_command=["notify"], meetings_dir=tmp_path / "meetings", export_poll=0.01)


EXAMPLES = Path(__file__).parent.parent / "examples"


@pytest.fixture
def start_doc():
    return json.loads((EXAMPLES / "teams-recording-start.json").read_text())


@pytest.fixture
def stop_doc():
    return json.loads((EXAMPLES / "teams-recording-stop.json").read_text())
