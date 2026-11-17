---
name: omamation
description: Build, change, test and inspect Omamation automations (triggers, conditions, actions as JSON, run by the omamation daemon on this machine). Use when the user asks to automate something on their desktop with Omamation, to see what an automation does or why it failed, or to change one.
---

# Omamation

Omamation runs automations on this machine. An automation is one JSON file:
`triggers` (when it starts), `conditions` (whether it may run), `actions`
(what it does, in order) and `on_error` (the catch, run when a step fails).
Work only through the `omamation` command; never edit the data folder.

## Look first
- `omamation list --json` — all automations, status, last run.
- `omamation show <id>` — what one does, as readable lines.
- `omamation blocks --json` — every trigger, condition and action with its options. Use only these.
- `omamation trace <id>` — what happened in the last run (`--all`, `--run <run_id>`).
- `omamation schema` — the JSON Schema of the file.

## Build or change
1. Write the JSON to a temporary file. Every step has a stable `id` (`^[a-z][a-z0-9_]*$`).
   Options go under `with`. Durations are strings: `20s`, `5m`, `1h`.
2. Pass values between steps only with references: `{{ steps.<id>.<field> }}`,
   `{{ trigger.<field> }}`, `{{ automation.name }}`, and in `on_error` also
   `{{ error.step }}` / `{{ error.message }}`. There is no expression language:
   anything that computes is an action.
3. Per action: `timeout` (default per block, mostly 60s), `retry` (default 1 for
   blocks with `retry_safe: true`, otherwise 0), `retry_delay` (default 5s).
4. Leave `on_error` out to get the standard catch (a critical notification with
   a Trace button), or write your own list of actions.
5. For a schedule use the `schedule` trigger (`at` or `every`, `days`, `missed`);
   add the condition `not_ran_today` for "once a day, at the first chance".
6. Blocks marked with an `integration` (e.g. `m365`: `calendar_event`,
   `calendar_now`) need that integration set up; `omamation m365 events` shows
   what the calendar integration sees.
7. `omamation validate <file>`, then `omamation save <file> --by agent`.

## Rules
- What an agent saves is a **concept**: it does not run until the user clicks
  Approve in the notification (`omamation approve <id>` only asks; you cannot approve).
  Tell the user it is waiting for approval in the bar.
- Changing an approved automation makes it a concept again. Say so before you save.
- Every save is a git commit in the data folder; nothing is lost.
- `omamation run <id>` runs an approved automation now (conditions are checked;
  `--skip-conditions` skips them). Check the result with `omamation trace <id>`.
- Never put secrets in an automation.
