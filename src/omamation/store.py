"""The automations on disk, their approvals, and their history.

Saves automations as JSON files in a git repository (one commit per change)
and keeps approvals as a hash of each automation without `enabled`."""

import hashlib
import json
import secrets
import subprocess
import time
from pathlib import Path

from . import model, paths
from .paths import write_atomic  # noqa: F401 - used by other modules through store

APPROVAL_REQUEST_SECONDS = 300


class StoreError(Exception):
    pass


def content_hash(doc: dict) -> str:
    body = {k: v for k, v in doc.items() if k not in ("enabled", "$schema")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class Store:
    def __init__(self, root: Path | None = None):
        self.root = root or paths.data_dir()
        self.dir = self.root / "automations"
        self.approvals_file = self.root / "approvals.json"

    # ---------------------------------------------------------------- reading

    def ids(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json")) if self.dir.exists() else []

    def path(self, automation_id: str) -> Path:
        return self.dir / f"{automation_id}.json"

    def get(self, automation_id: str) -> dict:
        path = self.path(automation_id)
        if not path.exists():
            raise StoreError(f"no automation {automation_id!r} (see `omamation list`)")
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise StoreError(f"{path} is not valid JSON: {exc}") from exc

    def approvals(self) -> dict:
        try:
            return json.loads(self.approvals_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def approved(self, doc: dict) -> bool:
        return self.approvals().get(doc["id"]) == content_hash(doc)

    def status(self, doc: dict) -> str:
        return "approved" if self.approved(doc) else "concept"

    # ---------------------------------------------------------------- writing

    def save(self, doc: dict, by: str) -> dict:
        doc = model.with_defaults(doc)
        problems = model.validate(doc)
        if problems:
            raise StoreError("not saved:\n" + "\n".join(f"  {p}" for p in problems))
        changed = not self.path(doc["id"]).exists() or self.get(doc["id"]) != doc
        if changed:
            write_atomic(self.path(doc["id"]), json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
            self.commit(f"Save {doc['id']} (by {by})")
        return doc

    def set_enabled(self, automation_id: str, enabled: bool, by: str):
        doc = self.get(automation_id)
        doc["enabled"] = enabled
        write_atomic(self.path(automation_id), json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        self.commit(f"{'Enable' if enabled else 'Disable'} {automation_id} (by {by})")

    def request_approval(self, automation_id: str) -> str:
        """A one-time token for the Approve button of a notification."""
        doc = self.get(automation_id)
        problems = model.validate(doc)
        if problems:
            raise StoreError("cannot approve an invalid automation:\n" + "\n".join(f"  {p}" for p in problems))
        token = secrets.token_urlsafe(16)
        request = {"token": token, "hash": content_hash(doc), "expires": time.time() + APPROVAL_REQUEST_SECONDS}
        write_atomic(paths.runtime_dir() / "approve" / f"{automation_id}.json", json.dumps(request))
        return token

    def approve(self, automation_id: str, token: str):
        request_file = paths.runtime_dir() / "approve" / f"{automation_id}.json"
        try:
            request = json.loads(request_file.read_text())
        except FileNotFoundError as exc:
            raise StoreError("no approval was requested for this automation") from exc
        request_file.unlink(missing_ok=True)
        doc = self.get(automation_id)
        if not secrets.compare_digest(request["token"], token):
            raise StoreError("approval token does not match")
        if time.time() > request["expires"]:
            raise StoreError("approval request expired; ask again")
        if request["hash"] != content_hash(doc):
            raise StoreError("the automation changed after approval was requested; ask again")
        approvals = self.approvals()
        approvals[automation_id] = content_hash(doc)
        write_atomic(self.approvals_file, json.dumps(approvals, indent=2, sort_keys=True) + "\n")
        self.commit(f"Approve {automation_id}")

    # ---------------------------------------------------------------- history

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=Omamation", "-c", "user.email=omamation@localhost", *args],
            capture_output=True, text=True,
        )

    def commit(self, message: str):
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            self._git("init", "-q", "-b", "main")
        self._git("add", "-A")
        result = self._git("commit", "-q", "-m", message)
        if result.returncode != 0 and "nothing to commit" not in result.stdout + result.stderr:
            raise StoreError(f"git commit failed: {result.stderr.strip()}")
