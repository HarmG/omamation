import json
import subprocess
import time

import pytest

from omamation import paths
from omamation.store import Store, StoreError


def log(store):
    return subprocess.run(["git", "-C", str(store.root), "log", "--format=%s"], capture_output=True, text=True).stdout.split("\n")


def approve(store, automation_id):
    store.approve(automation_id, store.request_approval(automation_id))


def test_new_automation_is_a_concept(dirs, start_doc):
    store = Store()
    saved = store.save(start_doc, by="agent")
    assert store.status(saved) == "concept"
    assert log(store)[0] == "Save teams-recording-start (by agent)"


def test_approval_needs_the_token(dirs, start_doc):
    store = Store()
    store.save(start_doc, by="agent")
    store.request_approval("teams-recording-start")
    with pytest.raises(StoreError, match="token does not match"):
        store.approve("teams-recording-start", "guess")
    assert store.status(store.get("teams-recording-start")) == "concept"


def test_approve_then_change_makes_it_a_concept_again(dirs, start_doc):
    store = Store()
    store.save(start_doc, by="harm")
    approve(store, "teams-recording-start")
    assert store.status(store.get("teams-recording-start")) == "approved"

    start_doc["triggers"][0]["with"]["for"] = "5s"
    store.save(start_doc, by="agent")
    assert store.status(store.get("teams-recording-start")) == "concept"
    assert log(store)[:3] == ["Save teams-recording-start (by agent)", "Approve teams-recording-start",
                              "Save teams-recording-start (by harm)"]


def test_editing_the_file_directly_also_loses_approval(dirs, start_doc):
    store = Store()
    store.save(start_doc, by="harm")
    approve(store, "teams-recording-start")
    doc = json.loads(store.path("teams-recording-start").read_text())
    doc["actions"][0]["with"]["title"] = "sneaky"
    store.path("teams-recording-start").write_text(json.dumps(doc))
    assert store.status(store.get("teams-recording-start")) == "concept"


def test_enable_and_disable_keep_approval(dirs, start_doc):
    store = Store()
    store.save(start_doc, by="harm")
    approve(store, "teams-recording-start")
    store.set_enabled("teams-recording-start", False, by="harm")
    doc = store.get("teams-recording-start")
    assert doc["enabled"] is False and store.status(doc) == "approved"


def test_change_after_request_is_refused(dirs, start_doc):
    store = Store()
    store.save(start_doc, by="harm")
    token = store.request_approval("teams-recording-start")
    start_doc["description"] = "changed in between"
    store.save(start_doc, by="agent")
    with pytest.raises(StoreError, match="changed after approval was requested"):
        store.approve("teams-recording-start", token)


def test_expired_request_is_refused(dirs, start_doc):
    store = Store()
    store.save(start_doc, by="harm")
    token = store.request_approval("teams-recording-start")
    request_file = paths.runtime_dir() / "approve" / "teams-recording-start.json"
    request = json.loads(request_file.read_text())
    request["expires"] = time.time() - 1
    request_file.write_text(json.dumps(request))
    with pytest.raises(StoreError, match="expired"):
        store.approve("teams-recording-start", token)


def test_invalid_automation_is_not_saved(dirs, start_doc):
    start_doc["actions"][0]["action"] = "nope"
    with pytest.raises(StoreError, match="not saved"):
        Store().save(start_doc, by="agent")
    assert Store().ids() == []
