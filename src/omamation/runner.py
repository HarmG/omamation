"""Running one automation: conditions, then the actions as a try, on_error as the catch.

Writes a trace per run: the trigger, each condition, each step's input,
output and error."""

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import model, paths
from .blocks import ACTIONS, CONDITIONS, Services, StepFailed
from .store import write_atomic
from .system import CommandMissing
from .values import MissingReference, resolve, seconds

SECRET_KEY = re.compile(r"token|password|secret|api_?key", re.IGNORECASE)
TRACE_DAYS = 30


def mask(value):
    """Replace values under secret-looking keys with *** and shorten long strings."""
    if isinstance(value, dict):
        return {k: "***" if SECRET_KEY.search(k) else mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "…"
    return value


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Run:
    def __init__(self, doc: dict, trigger: dict, dry_run: bool):
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.doc = doc
        self.trace = {
            "run_id": self.id,
            "automation": doc["id"],
            "trigger": trigger,
            "dry_run": dry_run,
            "started": now_iso(),
            "conditions": [],
            "steps": [],
            "catch": [],
            "result": None,
            "error": None,
        }
        self.context = {
            "automation": {"id": doc["id"], "name": doc["name"]},
            "trigger": trigger,
            "steps": {},
            "run": {"id": self.id},
        }


async def execute(doc: dict, trigger: dict, services: Services, dry_run: bool = False,
                  skip_conditions: bool = False) -> dict:
    run = Run(doc, trigger, dry_run)
    try:
        failure = None
        if not skip_conditions:
            passed, failure = await _conditions(run, services)
            if not passed and failure is None:
                run.trace["result"] = "skipped"
                return _finish(run)
        if failure is None:
            failure = await _steps(run, doc["actions"], services, dry_run, run.trace["steps"])
        if failure is None:
            run.trace["result"] = "success"
            return _finish(run)
        run.trace["result"] = "failed"
        run.trace["error"] = failure
        run.context["error"] = failure
        catch_failure = await _steps(run, doc.get("on_error", []), services, dry_run, run.trace["catch"])
        if catch_failure:
            run.trace["catch_error"] = catch_failure
    except Exception as exc:  # noqa: BLE001 - a bug in the engine still leaves a trace
        run.trace["result"] = "failed"
        run.trace["error"] = {"step": None, "message": f"engine error: {exc}"}
    return _finish(run)


async def _conditions(run: Run, services: Services) -> tuple[bool, dict | None]:
    """Check the conditions in order; returns (passed, failure), failure when a check could not run."""
    for step in run.doc.get("conditions", []):
        entry = {"id": step["id"], "condition": step["condition"], "started": now_iso()}
        run.trace["conditions"].append(entry)
        try:
            options = resolve(step.get("with", {}), run.context)
            passed, detail = await CONDITIONS[step["condition"]].check(
                options, services, {"automation": run.doc["id"], "run_id": run.id})
        except (StepFailed, CommandMissing, MissingReference, OSError, ValueError) as exc:
            entry.update(passed=False, detail=f"could not check: {exc}")
            return False, {"step": step["id"], "message": str(exc)}
        entry.update(passed=passed, detail=detail)
        if not passed:
            return False, None
    return True, None


async def _steps(run: Run, steps: list[dict], services: Services, dry_run: bool, log: list) -> dict | None:
    """Run steps in order; returns the failure, or None when all succeeded."""
    for step in steps:
        action = ACTIONS[step["action"]]
        entry = {"id": step["id"], "action": step["action"], "started": now_iso(), "attempts": 0}
        log.append(entry)
        try:
            options = resolve(step.get("with", {}), run.context)
        except MissingReference as exc:
            if not dry_run:
                entry.update(status="failed", error=str(exc), finished=now_iso())
                return {"step": step["id"], "message": str(exc)}
            # Dry run: keep the options unresolved when an earlier step has no output.
            options = step.get("with", {})
        entry["input"] = mask(options)
        if dry_run:
            entry.update(status="dry_run", finished=now_iso())
            run.context["steps"][step["id"]] = {}
            continue
        attempts = 1 + model.retries(step)
        delay = seconds(step.get("retry_delay", model.DEFAULT_RETRY_DELAY))
        limit = model.timeout(step)
        error = ""
        for attempt in range(attempts):
            entry["attempts"] = attempt + 1
            try:
                output = await asyncio.wait_for(
                    action.run(options, services, {"automation": run.doc["id"], "run_id": run.id}), limit)
                run.context["steps"][step["id"]] = output
                entry.update(status="success", output=mask(output), finished=now_iso())
                error = ""
                break
            except asyncio.TimeoutError:
                error = f"timed out after {step.get('timeout') or action.default_timeout}"
            except (StepFailed, CommandMissing, OSError, ValueError) as exc:
                error = str(exc)
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
        if error:
            entry.update(status="failed", error=error, finished=now_iso())
            return {"step": step["id"], "message": error}
    return None


def _finish(run: Run) -> dict:
    run.trace["finished"] = now_iso()
    folder = paths.traces_dir() / run.doc["id"]
    write_atomic(folder / f"{run.id}.json", json.dumps(run.trace, indent=2, ensure_ascii=False) + "\n")
    return run.trace


def traces(automation_id: str) -> list[Path]:
    folder = paths.traces_dir() / automation_id
    return sorted(folder.glob("*.json")) if folder.exists() else []


def prune_traces(days: int = TRACE_DAYS):
    cutoff = time.time() - timedelta(days=days).total_seconds()
    root = paths.traces_dir()
    if not root.exists():
        return
    for trace in root.glob("*/*.json"):
        if trace.stat().st_mtime < cutoff:
            trace.unlink(missing_ok=True)
