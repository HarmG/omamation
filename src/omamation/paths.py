"""Where Omamation keeps its files, following the XDG base directories.

Each location can be overridden with an environment variable."""

import os
from pathlib import Path


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def data_dir() -> Path:
    """Automations and approvals; a git repository."""
    override = os.environ.get("OMAMATION_DATA_DIR")
    return Path(override) if override else _xdg("XDG_DATA_HOME", ".local/share") / "omamation"


def state_dir() -> Path:
    """Traces of past runs."""
    override = os.environ.get("OMAMATION_STATE_DIR")
    return Path(override) if override else _xdg("XDG_STATE_HOME", ".local/state") / "omamation"


def runtime_dir() -> Path:
    """The daemon socket and the state file the bar widget reads."""
    override = os.environ.get("OMAMATION_RUNTIME_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "omamation"


def socket_path() -> Path:
    return runtime_dir() / "omamation.sock"


def state_file() -> Path:
    return runtime_dir() / "state.json"


def traces_dir() -> Path:
    return state_dir() / "traces"


def write_atomic(path: Path, text: str):
    """Write beside the target, then rename over it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
