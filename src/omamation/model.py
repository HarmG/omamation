"""Reading and checking automations.

`validate` is the single gate: the CLI, the daemon and agents all go through
it, so nothing runs that the editor could not show.
"""

import json
from importlib import resources

import jsonschema

from . import SCHEMA_VERSION
from .blocks import ACTIONS, CONDITIONS, TRIGGERS
from .values import references, seconds

DEFAULT_TIMEOUT = "60s"
DEFAULT_RETRY_DELAY = "5s"


def schema() -> dict:
    return json.loads(resources.files("omamation").joinpath("schema/automation.schema.json").read_text())


def default_on_error() -> list[dict]:
    """The standard catch: one notification that stays until it is dismissed."""
    return [{
        "id": "notify_failure",
        "action": "notify",
        "with": {
            "title": "{{ automation.name }} failed",
            "body": "{{ error.step }}: {{ error.message }}",
            "urgency": "critical",
            "buttons": [{"label": "Trace", "trace": True}],
        },
    }]


def with_defaults(doc: dict) -> dict:
    """Make the implicit explicit, so the file shows what will happen."""
    doc = json.loads(json.dumps(doc))
    doc.setdefault("schema_version", SCHEMA_VERSION)
    doc.setdefault("enabled", True)
    doc.setdefault("conditions", [])
    if "on_error" not in doc:
        doc["on_error"] = default_on_error()
    return doc


def retries(step: dict) -> int:
    if "retry" in step:
        return step["retry"]
    return 1 if ACTIONS[step["action"]].retry_safe else 0


def timeout(step: dict) -> float:
    return seconds(step.get("timeout") or ACTIONS[step["action"]].default_timeout or DEFAULT_TIMEOUT)


def validate(doc: dict) -> list[str]:
    """Every problem with an automation, as readable lines; empty when it is fine."""
    problems = [
        f"{'/'.join(map(str, e.absolute_path)) or '(top)'}: {e.message}"
        for e in sorted(jsonschema.Draft202012Validator(schema()).iter_errors(doc), key=lambda e: list(e.absolute_path))
    ]
    if problems:
        return problems

    seen: set[str] = set()

    def check_step(where: str, step: dict, registry: dict, kind: str):
        if step["id"] in seen:
            problems.append(f"{where}: id {step['id']!r} is used twice")
        seen.add(step["id"])
        block = registry.get(step[kind])
        if block is None:
            problems.append(f"{where}: unknown {kind} {step[kind]!r} (see `omamation blocks`)")
            return
        for error in jsonschema.Draft202012Validator(block.options).iter_errors(step.get("with", {})):
            path = "/".join(map(str, error.absolute_path))
            problems.append(f"{where}/with{'/' + path if path else ''}: {error.message}")

    for i, step in enumerate(doc["triggers"]):
        check_step(f"triggers/{i}", step, TRIGGERS, "trigger")
    for i, step in enumerate(doc.get("conditions", [])):
        check_step(f"conditions/{i}", step, CONDITIONS, "condition")

    def check_references(where: str, step: dict, earlier: list[str], in_catch: bool):
        for ref in references(step.get("with", {})):
            root, _, rest = ref.partition(".")
            if root == "steps":
                target = rest.split(".")[0]
                if target not in earlier:
                    problems.append(f"{where}: {{{{ {ref} }}}} refers to a step that does not come before it")
            elif root == "error" and not in_catch:
                problems.append(f"{where}: {{{{ {ref} }}}} only exists in on_error")
            elif root not in ("trigger", "automation", "error", "run"):
                problems.append(f"{where}: {{{{ {ref} }}}} is not a known reference")

    earlier: list[str] = []
    for i, step in enumerate(doc["actions"]):
        check_step(f"actions/{i}", step, ACTIONS, "action")
        check_references(f"actions/{i}", step, earlier, in_catch=False)
        earlier.append(step["id"])
    catch_earlier = list(earlier)
    for i, step in enumerate(doc.get("on_error", [])):
        check_step(f"on_error/{i}", step, ACTIONS, "action")
        # In the catch, earlier steps may not have run; their references resolve only if they did.
        check_references(f"on_error/{i}", step, catch_earlier, in_catch=True)
        catch_earlier.append(step["id"])
    return problems
