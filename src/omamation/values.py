"""Durations and references.

Parses durations (`20s`, `5m`, `1h`) and fills in references:
`{{ steps.<id>.<field> }}`, `{{ trigger.<field> }}`, `{{ error.<field> }}`
and `{{ automation.<field> }}`."""

import re

DURATION = re.compile(r"^(\d+)(s|m|h)$")
REFERENCE = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*\}\}")
ROOTS = ("steps", "trigger", "error", "automation")


class MissingReference(Exception):
    pass


def seconds(value: str | int | float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    match = DURATION.match(value)
    if not match:
        raise ValueError(f"not a duration: {value!r} (use e.g. 20s, 5m, 1h)")
    amount, unit = int(match[1]), match[2]
    return float(amount * {"s": 1, "m": 60, "h": 3600}[unit])


def references(value) -> list[str]:
    """Every reference path inside a value (strings, lists and dicts)."""
    if isinstance(value, str):
        return REFERENCE.findall(value)
    if isinstance(value, list):
        return [r for item in value for r in references(item)]
    if isinstance(value, dict):
        return [r for item in value.values() for r in references(item)]
    return []


def lookup(path: str, context: dict):
    current = context
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise MissingReference(f"{{{{ {path} }}}} has no value")
    return current


def resolve(value, context: dict):
    """Fill in references. A string that is exactly one reference keeps the referenced type."""
    if isinstance(value, str):
        whole = REFERENCE.fullmatch(value.strip())
        if whole:
            return lookup(whole[1], context)
        return REFERENCE.sub(lambda m: _text(lookup(m[1], context)), value)
    if isinstance(value, list):
        return [resolve(item, context) for item in value]
    if isinstance(value, dict):
        return {key: resolve(item, context) for key, item in value.items()}
    return value


def _text(value) -> str:
    return value if isinstance(value, str) else str(value)
