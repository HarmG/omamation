"""The `omamation` command. Every command takes --json, exits 0 or 1, and prints errors on stderr."""

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from . import __version__, model, paths, render, runner
from .blocks import catalogue
from .store import Store, StoreError

UNIT_NAME = "omamation.service"
PLUGIN_ID = "omamation"


def cli_argv() -> list[str]:
    """How to start this omamation again: for notification buttons and the bar widget."""
    found = shutil.which("omamation")
    argv0 = Path(sys.argv[0])
    if argv0.name == "omamation" and argv0.exists():
        return [str(argv0.resolve())]
    if found:
        return [found]
    return [sys.executable, "-m", "omamation"]


class Failure(Exception):
    pass


def out(args, data, text: str | None = None):
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    elif text is not None:
        print(text)


def notify_daemon(payload: dict) -> dict | None:
    from .daemon import request

    try:
        return asyncio.run(request(payload))
    except ConnectionError:
        return None


def load_document(source: str) -> dict:
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text()
        return json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise Failure(f"cannot read {source}: {exc}") from exc


# ------------------------------------------------------------------- commands


def cmd_list(args):
    store = Store()
    state = (notify_daemon({"command": "state"}) or {}).get("state")
    rows = []
    for automation_id in store.ids():
        try:
            doc = store.get(automation_id)
            row = {"id": automation_id, "name": doc.get("name", automation_id), "enabled": doc.get("enabled", True),
                   "status": store.status(doc) if not model.validate(doc) else "invalid"}
        except StoreError as exc:
            row = {"id": automation_id, "name": automation_id, "enabled": False, "status": "invalid", "problem": str(exc)}
        if state:
            live = next((a for a in state["automations"] if a["id"] == automation_id), {})
            row["running"] = live.get("running", False)
            row["last_run"] = live.get("last_run")
        rows.append(row)
    lines = [f"{r['id']:<28} {'on ' if r['enabled'] else 'off'}  {r['status']:<9} "
             f"{(r.get('last_run') or {}).get('result') or '-':<8} {r['name']}" for r in rows]
    if state is None and not args.json:
        lines.append("(daemon not running: no run information)")
    out(args, {"automations": rows, "daemon_running": state is not None}, "\n".join(lines) or "No automations yet.")


def cmd_show(args):
    store = Store()
    doc = store.get(args.id)
    status = store.status(doc)
    out(args, {"automation": doc, "status": status, "lines": render.lines(doc)}, render.text(doc, status))


def cmd_blocks(args):
    data = catalogue()
    text = []
    for kind in ("triggers", "conditions", "actions"):
        text.append(kind)
        for block in data[kind]:
            options = ", ".join(block["options"].get("properties", {}).keys())
            text.append(f"  {block['type']:<24} {block['description']}" + (f"  [{options}]" if options else ""))
    out(args, data, "\n".join(text))


def cmd_schema(args):
    print(json.dumps(model.schema(), indent=2))


def cmd_validate(args):
    doc = model.with_defaults(load_document(args.file))
    problems = model.validate(doc)
    out(args, {"valid": not problems, "problems": problems}, "valid" if not problems else "\n".join(problems))
    if problems:
        raise Failure(f"{len(problems)} problem(s)")


def cmd_save(args):
    store = Store()
    doc = load_document(args.file)
    was_approved = store.path(doc.get("id", "")).exists() and store.approved(store.get(doc["id"]))
    saved = store.save(doc, by=args.by)
    status = store.status(saved)
    notify_daemon({"command": "reload"})
    note = ""
    if was_approved and status != "approved":
        note = " It changed, so it is a concept again and does not run until it is approved."
    elif status != "approved":
        note = " It is a concept: it runs only after it is approved in the bar (Approve)."
    out(args, {"saved": saved["id"], "status": status}, f"Saved {saved['id']} ({status}).{note}")


def cmd_enable(args, enabled: bool):
    Store().set_enabled(args.id, enabled, by=args.by)
    notify_daemon({"command": "reload"})
    out(args, {"id": args.id, "enabled": enabled}, f"{args.id} {'enabled' if enabled else 'disabled'}.")


def cmd_run(args):
    reply = notify_daemon({"command": "run", "id": args.id, "source": args.source,
                           "skip_conditions": args.skip_conditions})
    if reply is None:
        raise Failure("the omamation daemon is not running (systemctl --user status omamation)")
    if not reply.get("ok"):
        raise Failure(reply.get("error", "run failed"))
    out(args, reply, f"{args.id}: {reply['result']} (see `omamation trace {args.id}`)")


def cmd_approve(args):
    store = Store()
    if args.token:
        store.approve(args.id, args.token)
        notify_daemon({"command": "reload"})
        subprocess.run(["omarchy-notification-send", "--app-name", "Omamation", f"Approved {args.id}"], check=False)
        out(args, {"approved": args.id}, f"Approved {args.id}.")
        return
    # Approval is a click in a notification: an agent can ask for it, only a person can give it.
    doc = store.get(args.id)
    token = store.request_approval(args.id)
    result = subprocess.run([
        "omarchy-notification-send", "--app-name", "Omamation", "-u", "critical",
        f"Approve {doc['name']}?",
        "It will run on its own from now on. Check it in the bar first (omamation show " + args.id + ").",
        "--action", "Approve", *cli_argv(), "approve", args.id, "--token", token,
    ], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise Failure(f"could not show the approval notification: {result.stderr.strip()}")
    out(args, {"requested": args.id}, f"Approval requested: click Approve in the notification (valid 5 minutes).")


def cmd_trace(args):
    files = runner.traces(args.id)
    if args.run:
        files = [f for f in files if f.stem == args.run]
    elif not args.all:
        files = files[-1:]
    if not files:
        raise Failure(f"no traces for {args.id}")
    traces = [json.loads(f.read_text()) for f in files]
    if args.json:
        print(json.dumps(traces if args.all else traces[0], indent=2, ensure_ascii=False))
        return
    for trace in traces:
        print(f"{trace['automation']}  run {trace['run_id']}  {trace['result']}"
              + ("  (dry run)" if trace.get("dry_run") else ""))
        print(f"  trigger   {trace['trigger'].get('type')}  {trace['trigger'].get('time', '')}")
        for c in trace["conditions"]:
            print(f"  if        {c['condition']}  {'yes' if c['passed'] else 'no'}  ({c.get('detail', '')})  #{c['id']}")
        for label, steps in (("do", trace["steps"]), ("catch", trace["catch"])):
            for s in steps:
                detail = s.get("error") or json.dumps(s.get("output", {}), ensure_ascii=False)[:200]
                print(f"  {label:<9} {s['action']}  {s.get('status')}  attempts {s['attempts']}  {detail}  #{s['id']}")
        if trace.get("error"):
            print(f"  error     {trace['error']['step']}: {trace['error']['message']}")
        print()


def cmd_daemon(args):
    from .daemon import Daemon, build_services

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(Daemon(build_services(cli_argv()), dry_run=args.dry_run).serve())


def package_dir(name: str) -> Path:
    return Path(str(resources.files("omamation").joinpath(name)))


def cmd_setup(args):
    unit_dir = Path.home() / ".config/systemd/user"
    unit = unit_dir / UNIT_NAME
    plugin_link = Path.home() / ".config/omarchy/plugins" / PLUGIN_ID
    if args.remove:
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
        unit.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        if plugin_link.is_symlink():
            plugin_link.unlink()
        out(args, {"removed": True}, "Removed the service and the bar widget link. Your automations stay in "
            f"{paths.data_dir()}.")
        return
    template = package_dir("systemd").joinpath(UNIT_NAME).read_text()
    exec_start = " ".join(cli_argv()) + " daemon" + (" --dry-run" if args.dry_run else "")
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit.write_text(template.replace("@EXEC_START@", exec_start))
    plugin_link.parent.mkdir(parents=True, exist_ok=True)
    if plugin_link.is_symlink() or not plugin_link.exists():
        plugin_link.unlink(missing_ok=True)
        plugin_link.symlink_to(package_dir("plugin"))
    else:
        raise Failure(f"{plugin_link} exists and is not a link; remove it first")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", UNIT_NAME], check=True)
    subprocess.run(["systemctl", "--user", "restart", UNIT_NAME], check=True)
    out(args, {"unit": str(unit), "plugin": str(plugin_link)},
        f"Service {UNIT_NAME} started{' in dry-run mode' if args.dry_run else ''}.\n"
        f"Bar widget linked at {plugin_link}. Not in the bar yet? Run: omarchy bar put omamation")


def cmd_skill(args):
    target = Path(args.dir).expanduser() / "omamation"
    target.mkdir(parents=True, exist_ok=True)
    source = package_dir("skill").joinpath("SKILL.md")
    (target / "SKILL.md").write_text(source.read_text())
    out(args, {"installed": str(target / "SKILL.md")}, f"Installed the skill at {target / 'SKILL.md'}.")


# ------------------------------------------------------------------- parser


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="omamation", description="A small automation engine for Omarchy.")
    p.add_argument("--version", action="version", version=f"omamation {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def command(name, func, help_text):
        c = sub.add_parser(name, help=help_text)
        c.add_argument("--json", action="store_true", help="machine-readable output")
        c.set_defaults(func=func)
        return c

    command("list", cmd_list, "all automations with status and last run")
    command("show", cmd_show, "an automation as readable lines").add_argument("id")
    command("blocks", cmd_blocks, "every trigger, condition and action with its options")
    command("schema", cmd_schema, "the JSON Schema of an automation file")
    command("validate", cmd_validate, "check an automation file without saving").add_argument("file", help="path or - for stdin")
    c = command("save", cmd_save, "validate and save an automation (new or changed ones are concepts)")
    c.add_argument("file", help="path or - for stdin")
    c.add_argument("--by", default="cli", help="who saves it, for the history (e.g. agent, harm)")
    for name, enabled in (("enable", True), ("disable", False)):
        c = command(name, lambda a, e=enabled: cmd_enable(a, e), f"{name} an automation")
        c.add_argument("id")
        c.add_argument("--by", default="cli")
    c = command("run", cmd_run, "run an automation now (conditions are checked)")
    c.add_argument("id")
    c.add_argument("--source", default="cli", help=argparse.SUPPRESS)
    c.add_argument("--skip-conditions", action="store_true")
    c = command("approve", cmd_approve, "ask for approval with a notification button")
    c.add_argument("id")
    c.add_argument("--token", help=argparse.SUPPRESS)
    c = command("trace", cmd_trace, "what happened in the last run")
    c.add_argument("id")
    c.add_argument("--run", help="a specific run id")
    c.add_argument("--all", action="store_true", help="every kept run")
    command("daemon", cmd_daemon, "run the engine (systemd starts this)").add_argument("--dry-run", action="store_true")
    c = command("setup", cmd_setup, "install the user service and link the bar widget")
    c.add_argument("--remove", action="store_true")
    c.add_argument("--dry-run", action="store_true", help="install the service in dry-run mode")
    skill = command("skill", None, "agent skill")
    skill_sub = skill.add_subparsers(dest="skill_command", required=True)
    install = skill_sub.add_parser("install", help="install the agent skill")
    install.add_argument("--dir", required=True, help="skills folder, e.g. ~/.claude/skills")
    install.add_argument("--json", action="store_true")
    install.set_defaults(func=cmd_skill)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        args.func(args)
        return 0
    except (Failure, StoreError, OSError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
        else:
            print(f"omamation: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
