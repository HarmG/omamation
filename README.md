# Omamation

A small automation engine for [Omarchy](https://omarchy.org). An automation is
a JSON file with **triggers** (when it starts), **conditions** (whether it may
run), **actions** (what it does) and **on_error** (what happens when a step
fails). A daemon runs them; a CLI and a bar widget show and control them; agents
build them through the same CLI.

```
Teams recording start  [teams-recording-start]  (enabled, approved)
  Starts a Voxtype meeting recording when a Teams meeting starts.
  when      microphone_in_use  app: Chromium  for: 20s
  if        window_open  class: chrome-teams.cloud.microsoft__-Default
  if        voxtype_meeting  state: idle
  do        voxtype_meeting_start  title: Teams {{ trigger.time }}  (timeout 30s, retry 1×)
  do        notify  title: Recording started  body: {{ steps.record.title }}  buttons: Stop → teams-recording-stop
  on error  notify  title: {{ automation.name }} failed  body: {{ error.step }}: {{ error.message }}  urgency: critical  buttons: Trace → trace
```

## Install

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/), and Omarchy (Hyprland,
PipeWire, the Omarchy shell).

```sh
uv tool install git+https://github.com/HarmG/omamation
omamation setup          # user service + bar widget link
```

Then put **Omamation** in the bar with `omarchy bar put omamation`.
`omamation setup --remove` undoes it; your
automations stay.

## How it works

- **Everything is a block.** Built-in triggers, conditions and actions, each
  with a manifest (options as JSON Schema, outputs, whether it is safe to run
  twice). `omamation blocks` lists them. There is no hidden logic: what the
  file says is what runs.
- **References, no expressions.** `{{ steps.<id>.<field> }}`,
  `{{ trigger.<field> }}`, `{{ error.message }}`. Anything that computes is an
  action of its own.
- **Try/catch.** `actions` is the try, `on_error` the catch. Without your own
  catch you get a critical notification with a **Trace** button. Each action
  has a `timeout` and a `retry` (once by default, unless the block is not safe
  to repeat, like a notification).
- **Approval.** A new or changed automation is a *concept* and does not run
  until you click **Approve** in a notification. The approval is a hash of the
  content, so any later change (also by hand) makes it a concept again.
- **History.** The data folder (`~/.local/share/omamation`) is a git
  repository: every save, approval and switch is a commit.
- **Traces.** Every run is kept for 30 days in `~/.local/state/omamation/traces`.
  `omamation trace <id>` shows what each condition saw and each step did.
- **Run mode single.** A trigger that fires while the automation runs is ignored.
- **Schedules survive restarts.** The last time each `schedule` trigger fired is
  kept in `~/.local/state/omamation/schedule.json`, so a restart does not fire it
  twice. A time missed by more than 2 minutes (asleep, off) is skipped, or run
  once when back with `missed: catch_up`.
- The CLI and the widget talk to the daemon over a unix socket in
  `$XDG_RUNTIME_DIR/omamation/`, never a TCP port.

## Blocks

| Kind | Block | Options |
|---|---|---|
| trigger | `microphone_in_use` | `app` (start of the PipeWire app name), `for` |
| trigger | `microphone_released` | `app`, `for` |
| trigger | `schedule` | `at` (`["HH:MM"]`) or `every` (with `first_after`), `days` (`mon`..`sun`), `missed`: `skip` or `catch_up` |
| trigger | `manual` | — (Run now, `omamation run`, a notification button) |
| condition | `window_open` | `class` (Hyprland window class) |
| condition | `voxtype_meeting` | `state`: `idle` or `recording` |
| condition | `not_ran_today` | — (no successful run of this automation today) |
| action | `run_command` | `command` (argv list) |
| action | `notify` | `title`, `body`, `urgency`, `buttons` (`run` an automation, or `trace`) |
| action | `voxtype_meeting_start` | `title` |
| action | `voxtype_meeting_stop` | `folder` (default `~/Meetings`) |

## Integrations

An integration adds blocks for one service. Its settings live in
`~/.config/omamation/integrations.json`; `omamation blocks` marks its blocks.

### m365 (Outlook calendar, read only)

Install with the extra: `uv tool install 'omamation[m365]'`. It reuses an
existing MSAL login (a confidential client with a persisted token cache) and
never starts a login itself:

```json
{
  "m365": {
    "config_env": "~/.config/my-tool/config.env",
    "token_cache": "~/.local/share/my-tool/token_cache.bin",
    "secret": ["service", "my-tool", "key", "client_secret"]
  }
}
```

`config_env` holds `MS_TENANT_ID` and `MS_CLIENT_ID`; `secret` are the
`secret-tool lookup` attributes of the client secret. The token needs
`Calendars.Read`. The calendar (past 12 hours, next 24) is fetched every 5
minutes while a `calendar_event` trigger is active; a fetch error shows in the
bar and the last fetched calendar keeps counting.

| Kind | Block | Options |
|---|---|---|
| trigger | `calendar_event` | `before` (duration), filters `subject` (part of it), `online`, `with_others`, `skip_show_as` (default `free`, `oof`) |
| action | `calendar_now` | `fallback`, same filters; outputs the `subject` of the item happening now, never fails |

`calendar_event` fires once per item at `start − before`, or right away when
that moment passed but the item has not started; it fires again when the item
moves. Cancelled and all-day items never count.

## CLI

| Command | What it does |
|---|---|
| `omamation list` | all automations with status and last run |
| `omamation show <id>` | the readable view |
| `omamation blocks` | every block and its options |
| `omamation schema` | the JSON Schema (also for editor completion via `$schema`) |
| `omamation validate <file>` | check without saving |
| `omamation save <file> [--by who]` | validate and save (a concept until approved) |
| `omamation enable/disable <id>` | switch on or off (keeps the approval) |
| `omamation approve <id>` | ask for approval with a notification button |
| `omamation run <id>` | run now; conditions are checked |
| `omamation trace <id>` | the last run, step by step |
| `omamation daemon [--dry-run]` | the engine; dry run only writes traces |
| `omamation setup [--remove]` | user service and bar widget |
| `omamation m365 events` | the calendar items the m365 integration sees |
| `omamation skill install [--dir <skills>]` | link the agent skill into `~/.claude/skills` (follows updates) |

Every command takes `--json`, exits 0 or 1, and prints errors on stderr.

## Examples

[`examples/`](examples) holds the two automations that record Teams meetings
with [Voxtype](https://voxtype.io): start when Chromium has
used the microphone for 20 seconds while the Teams web app is open, stop when
it has been free for 60 seconds.

```sh
omamation save examples/teams-recording-start.json
omamation approve teams-recording-start
```

## Development

```sh
uv sync
uv run pytest
uv run omamation daemon --dry-run
```

Tests use fake `pactl`, `hyprctl`, `voxtype` and notifications; nothing on the
desktop is touched.

## License

MIT
