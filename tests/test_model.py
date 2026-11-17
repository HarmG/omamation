import json

import pytest

from omamation import model
from omamation.values import MissingReference, resolve, seconds


def test_examples_are_valid(start_doc, stop_doc):
    assert model.validate(model.with_defaults(start_doc)) == []
    assert model.validate(model.with_defaults(stop_doc)) == []


def test_default_catch_is_written_out(start_doc):
    doc = model.with_defaults(start_doc)
    assert doc["on_error"][0]["action"] == "notify"
    assert doc["on_error"][0]["with"]["urgency"] == "critical"


def test_own_catch_is_kept(start_doc):
    start_doc["on_error"] = [{"id": "log", "action": "run_command", "with": {"command": ["true"]}}]
    assert model.with_defaults(start_doc)["on_error"][0]["id"] == "log"


@pytest.mark.parametrize("change, expected", [
    (lambda d: d["actions"][0].update(action="nope"), "unknown action 'nope'"),
    (lambda d: d["triggers"][0]["with"].update(extra=1), "Additional properties"),
    (lambda d: d["triggers"][0]["with"].update({"for": "20 seconds"}), "does not match"),
    (lambda d: d["actions"][1].update(id="record"), "used twice"),
    (lambda d: d["actions"][0]["with"].update(title="{{ steps.tell.title }}"), "does not come before"),
    (lambda d: d["actions"][0]["with"].update(title="{{ error.message }}"), "only exists in on_error"),
    (lambda d: d["actions"][0]["with"].update(title="{{ secrets.x }}"), "not a known reference"),
    (lambda d: d.update(id="Bad Id"), "does not match"),
])
def test_validate_reports_problems(start_doc, change, expected):
    doc = model.with_defaults(start_doc)
    change(doc)
    problems = model.validate(doc)
    assert any(expected in p for p in problems), problems


def test_retry_defaults_follow_retry_safe(start_doc):
    record, tell = start_doc["actions"]
    assert model.retries(record) == 1   # voxtype_meeting_start is safe to repeat
    assert model.retries(tell) == 0     # a notification is not
    tell["retry"] = 2
    assert model.retries(tell) == 2


def test_durations():
    assert seconds("20s") == 20
    assert seconds("5m") == 300
    assert seconds("1h") == 3600
    with pytest.raises(ValueError):
        seconds("soon")


def test_references_keep_type_and_fill_text():
    context = {"steps": {"a": {"n": 3, "file": "/x.md"}}, "trigger": {"time": "10:00"}}
    assert resolve("{{ steps.a.n }}", context) == 3
    assert resolve("Saved {{ steps.a.file }} at {{trigger.time}}", context) == "Saved /x.md at 10:00"
    assert resolve({"list": ["{{ steps.a.n }}"]}, context) == {"list": [3]}
    with pytest.raises(MissingReference):
        resolve("{{ steps.b.file }}", context)


def test_schema_file_is_valid_json_schema():
    import jsonschema

    jsonschema.Draft202012Validator.check_schema(model.schema())
