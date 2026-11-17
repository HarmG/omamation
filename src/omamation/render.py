"""The readable view of an automation: what `omamation show` prints and the bar widget shows."""

import json

from . import model


def _options(options: dict) -> str:
    parts = []
    for key, value in options.items():
        if key == "buttons":
            labels = [b["label"] + (f" → {b['run']}" if b.get("run") else " → trace") for b in value]
            parts.append("buttons: " + ", ".join(labels))
        elif isinstance(value, (dict, list)):
            parts.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            parts.append(f"{key}: {value}")
    return "  ".join(parts)


def _action_extras(step: dict) -> str:
    extras = []
    if "timeout" in step:
        extras.append(f"timeout {step['timeout']}")
    retry = model.retries(step) if step["action"] in model.ACTIONS else step.get("retry", 0)
    if retry:
        extras.append(f"retry {retry}×" + (f" after {step['retry_delay']}" if "retry_delay" in step else ""))
    return f"  ({', '.join(extras)})" if extras else ""


def lines(doc: dict) -> list[dict]:
    """Rows of (label, step id, block, options), shared by the text and the widget."""
    rows = []
    for step in doc.get("triggers", []):
        rows.append({"label": "when", "id": step["id"], "block": step["trigger"], "text": _options(step.get("with", {}))})
    for step in doc.get("conditions", []):
        rows.append({"label": "if", "id": step["id"], "block": step["condition"], "text": _options(step.get("with", {}))})
    for step in doc.get("actions", []):
        rows.append({"label": "do", "id": step["id"], "block": step["action"],
                     "text": _options(step.get("with", {})) + _action_extras(step)})
    for step in doc.get("on_error", []):
        rows.append({"label": "on error", "id": step["id"], "block": step["action"],
                     "text": _options(step.get("with", {})) + _action_extras(step)})
    return rows


def text(doc: dict, status: str) -> str:
    state = ("enabled" if doc.get("enabled", True) else "disabled") + ", " + status
    out = [f"{doc['name']}  [{doc['id']}]  ({state})"]
    if doc.get("description"):
        out.append(f"  {doc['description']}")
    width = max(len(r["label"]) for r in lines(doc))
    for row in lines(doc):
        out.append(f"  {row['label']:<{width}}  {row['block']}  {row['text']}".rstrip() + f"   #{row['id']}")
    return "\n".join(out)
