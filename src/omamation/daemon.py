"""The engine: watches triggers of approved, enabled automations and runs them.

The CLI and the bar widget talk to it over a unix socket in the runtime
directory (not a TCP port, which any web page in a browser could reach).
The widget reads `state.json`, which the daemon rewrites after every change.
"""

import asyncio
import json
import logging
import os
import signal
from datetime import datetime

from . import paths, render, runner
from .blocks import TRIGGERS, MicrophoneSource, Services
from .store import Store, StoreError, write_atomic
from .system import System

log = logging.getLogger("omamation")
RESCAN_SECONDS = 10


class Daemon:
    def __init__(self, services: Services, store: Store | None = None, dry_run: bool = False):
        self.services = services
        self.store = store or Store()
        self.dry_run = dry_run
        self.started = datetime.now().astimezone().isoformat(timespec="seconds")
        self.stops: list = []
        self.running: dict[str, asyncio.Task] = {}
        self.loaded: dict[str, dict] = {}
        self.problems: dict[str, str] = {}
        self.signature = None

    # ---------------------------------------------------------------- automations

    def _scan_signature(self):
        files = [self.store.approvals_file, *self.store.dir.glob("*.json")] if self.store.dir.exists() else []
        return tuple(sorted((str(p), p.stat().st_mtime_ns) for p in files if p.exists()))

    def reload(self):
        for stop in self.stops:
            stop()
        self.stops, self.loaded, self.problems = [], {}, {}
        for automation_id in self.store.ids():
            try:
                doc = self.store.get(automation_id)
            except StoreError as exc:
                self.problems[automation_id] = str(exc)
                continue
            from .model import validate

            problems = validate(doc)
            if problems:
                self.problems[automation_id] = "; ".join(problems)
                continue
            self.loaded[automation_id] = doc
            if doc.get("enabled", True) and self.store.approved(doc):
                for step in doc["triggers"]:
                    self.stops.append(TRIGGERS[step["trigger"]].start(
                        step.get("with", {}), self.services, self._fire_for(automation_id, step)))
        self.signature = self._scan_signature()
        log.info("loaded %d automations (%d active)", len(self.loaded), len(self.stops))
        self.write_state()

    def _fire_for(self, automation_id: str, step: dict):
        def fire(data: dict):
            trigger = {"id": step["id"], "type": step["trigger"],
                       "time": datetime.now().strftime("%Y-%m-%d %H:%M"), **data}
            self.start_run(automation_id, trigger)
        return fire

    def start_run(self, automation_id: str, trigger: dict, skip_conditions: bool = False) -> str:
        doc = self.loaded.get(automation_id)
        if doc is None:
            raise StoreError(f"no valid automation {automation_id!r}")
        if not self.store.approved(doc):
            raise StoreError(f"{automation_id} is not approved; approve it in the bar first")
        # Run mode single: a trigger that fires during a run is ignored.
        if automation_id in self.running:
            log.info("%s is already running; trigger %s ignored", automation_id, trigger.get("id"))
            return "already running"
        trigger = {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), **trigger}
        task = asyncio.create_task(self._run(doc, trigger, skip_conditions))
        self.running[automation_id] = task
        self.write_state()
        return "started"

    async def _run(self, doc: dict, trigger: dict, skip_conditions: bool):
        try:
            trace = await runner.execute(doc, trigger, self.services, self.dry_run, skip_conditions)
            level = logging.ERROR if trace["result"] == "failed" else logging.INFO
            log.log(level, "%s: %s%s", doc["id"], trace["result"],
                    f" at {trace['error']['step']}: {trace['error']['message']}" if trace.get("error") else "")
        finally:
            self.running.pop(doc["id"], None)
            self.write_state()

    # ---------------------------------------------------------------- state for the widget

    def _last(self, automation_id: str) -> tuple[dict | None, dict | None]:
        """The latest run, and the latest run that got past its conditions."""
        latest, relevant = None, None
        for path in reversed(runner.traces(automation_id)):
            try:
                trace = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            summary = {k: trace.get(k) for k in ("run_id", "result", "started", "finished", "error", "dry_run")}
            latest = latest or summary
            if trace.get("result") != "skipped":
                relevant = summary
                break
        return latest, relevant

    def state(self) -> dict:
        automations = []
        for automation_id in self.store.ids():
            doc = self.loaded.get(automation_id)
            latest, relevant = self._last(automation_id)
            entry = {"id": automation_id, "running": automation_id in self.running,
                     "last_run": latest, "last_result": relevant}
            if doc:
                entry.update(name=doc["name"], description=doc.get("description", ""),
                             enabled=doc.get("enabled", True), status=self.store.status(doc),
                             lines=render.lines(doc))
            else:
                entry.update(name=automation_id, status="invalid", enabled=False,
                             problem=self.problems.get(automation_id, ""))
            automations.append(entry)
        errors = [e for e in [self.services.microphone.error] if e]
        failed = any(a.get("last_result") and a["last_result"]["result"] == "failed" for a in automations)
        return {
            "daemon": {"pid": os.getpid(), "started": self.started, "dry_run": self.dry_run, "errors": errors},
            "cli": self.services.cli,
            "attention": failed or bool(errors) or any(a["status"] == "invalid" for a in automations),
            "automations": automations,
        }

    def write_state(self):
        write_atomic(paths.state_file(), json.dumps(self.state(), indent=2, ensure_ascii=False) + "\n")

    # ---------------------------------------------------------------- socket

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request = json.loads(await reader.readline() or b"{}")
            command = request.get("command")
            if command == "reload":
                self.reload()
                reply = {"ok": True}
            elif command == "run":
                result = self.start_run(request["id"], {"id": "manual", "type": "manual",
                                                        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                                        "source": request.get("source", "cli")},
                                        skip_conditions=bool(request.get("skip_conditions")))
                reply = {"ok": True, "result": result}
            elif command == "state":
                reply = {"ok": True, "state": self.state()}
            else:
                reply = {"ok": False, "error": f"unknown command {command!r}"}
        except (StoreError, KeyError, json.JSONDecodeError) as exc:
            reply = {"ok": False, "error": str(exc)}
        writer.write((json.dumps(reply) + "\n").encode())
        await writer.drain()
        writer.close()

    async def serve(self):
        paths.runtime_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        socket = paths.socket_path()
        socket.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self.handle, path=str(socket))
        os.chmod(socket, 0o600)
        runner.prune_traces()
        self.reload()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        loop.add_signal_handler(signal.SIGHUP, self.reload)
        log.info("omamation daemon started%s", " (dry run: actions are only logged)" if self.dry_run else "")
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), RESCAN_SECONDS)
                except asyncio.TimeoutError:
                    # Files changed outside the CLI (an agent, an editor): load them again,
                    # so a changed automation loses its approval right away.
                    if self._scan_signature() != self.signature:
                        self.reload()
                    else:
                        self.write_state()
        finally:
            for stop_trigger in self.stops:
                stop_trigger()
            for task in self.running.values():
                task.cancel()
            await self.services.microphone.stop()
            server.close()
            socket.unlink(missing_ok=True)
            paths.state_file().unlink(missing_ok=True)


def build_services(cli: list[str]) -> Services:
    system = System()
    return Services(system=system, microphone=MicrophoneSource(system), cli=cli)


async def request(payload: dict, timeout: float = 5) -> dict:
    """Send one command to the running daemon."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(paths.socket_path())), timeout)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise ConnectionError("the omamation daemon is not running (systemctl --user status omamation)") from exc
    writer.write((json.dumps(payload) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout)
    writer.close()
    return json.loads(line or b"{}")
