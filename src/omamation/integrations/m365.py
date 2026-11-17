"""Integration m365: the Outlook calendar through Microsoft Graph, read only.

Uses an existing MSAL login (client id, tenant, secret from the keyring and a
shared token cache) set in `~/.config/omamation/integrations.json`; it never
starts a login of its own."""

import asyncio
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .. import paths
from ..blocks import DURATION_SCHEMA, Action, Services, Trigger
from ..paths import write_atomic
from ..values import seconds

NAME = "m365"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["https://graph.microsoft.com/Calendars.Read"]
POLL_SECONDS = 300
LOOKAHEAD = timedelta(hours=24)
FRESH = timedelta(minutes=10)
NOW_LEAD = timedelta(minutes=5)

FILTER_OPTIONS = {
    "subject": {"type": "string", "minLength": 1, "description": "Part of the subject (case does not matter)"},
    "online": {"type": "boolean", "description": "Only Teams meetings"},
    "with_others": {"type": "boolean", "description": "Only items with at least one other person"},
    "skip_show_as": {"type": "array", "items": {"enum": ["free", "tentative", "busy", "oof", "workingElsewhere"]},
                     "description": "Skip items shown as these (default free, oof)"},
}


class IntegrationError(Exception):
    pass


# --------------------------------------------------------------------------- settings and Graph


def settings() -> dict:
    """The m365 part of ~/.config/omamation/integrations.json."""
    file = paths.config_dir() / "integrations.json"
    try:
        data = json.loads(file.read_text()).get(NAME)
    except FileNotFoundError as exc:
        raise IntegrationError(f"m365 is not set up: {file} is missing (see README, Integrations)") from exc
    except json.JSONDecodeError as exc:
        raise IntegrationError(f"{file} is not valid JSON: {exc}") from exc
    if not data:
        raise IntegrationError(f"m365 is not set up in {file}")
    return data


def _env_file(path: Path) -> dict:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"')
    return values


def _secret(attributes: list[str]) -> str:
    result = subprocess.run(["secret-tool", "lookup", *attributes], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise IntegrationError("m365: the client secret is not in the keyring")
    return result.stdout.strip()


def _token() -> tuple[str, str]:
    """An access token for Calendars.Read from the token cache, and the account's address."""
    try:
        import msal
        from msal_extensions import FilePersistence, PersistedTokenCache
    except ImportError as exc:
        raise IntegrationError("m365 needs msal: uv tool install --reinstall 'omamation[m365]'") from exc
    conf = settings()
    env = _env_file(Path(conf["config_env"]).expanduser())
    tenant, client = env.get(conf.get("tenant_key", "MS_TENANT_ID")), env.get(conf.get("client_key", "MS_CLIENT_ID"))
    if not tenant or not client:
        raise IntegrationError(f"m365: tenant or client id missing in {conf['config_env']}")
    app = msal.ConfidentialClientApplication(
        client, client_credential=_secret(conf["secret"]),
        authority=f"https://login.microsoftonline.com/{tenant}",
        token_cache=PersistedTokenCache(FilePersistence(str(Path(conf["token_cache"]).expanduser()))),
    )
    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    if not result or "access_token" not in result:
        reason = (result or {}).get("error_description", "no account in the token cache").splitlines()[0]
        raise IntegrationError(f"m365: no token for Calendars.Read ({reason}); log in again with the tool that owns the login")
    return result["access_token"], accounts[0].get("username", "")


def _local(value: dict | None) -> datetime | None:
    """A Graph dateTimeTimeZone in UTC as a local time without a time zone."""
    if not value or not value.get("dateTime"):
        return None
    moment = datetime.fromisoformat(value["dateTime"][:19]).replace(tzinfo=timezone.utc)
    return moment.astimezone().replace(tzinfo=None)


def to_event(item: dict, me: str) -> dict:
    """The fields of one Graph event that the blocks use."""
    me = me.lower()
    people = {(a.get("emailAddress") or {}).get("address", "").lower() for a in item.get("attendees") or []}
    people.add(((item.get("organizer") or {}).get("emailAddress") or {}).get("address", "").lower())
    people.discard("")
    people.discard(me)
    return {
        "event_id": item.get("id", ""),
        "subject": item.get("subject") or "(no subject)",
        "start": _local(item.get("start")),
        "end": _local(item.get("end")),
        "online": bool(item.get("isOnlineMeeting")) or item.get("onlineMeetingProvider") == "teamsForBusiness",
        "others": len(people),
        "show_as": item.get("showAs") or "busy",
        "cancelled": bool(item.get("isCancelled")),
        "all_day": bool(item.get("isAllDay")),
        "join_url": (item.get("onlineMeeting") or {}).get("joinUrl") or "",
        "location": (item.get("location") or {}).get("displayName") or "",
    }


def fetch_graph(start: datetime, end: datetime) -> list[dict]:
    """Calendar items between two local times (blocking; run it in a thread)."""
    token, me = _token()
    query = urllib.parse.urlencode({
        "startDateTime": start.astimezone().isoformat(), "endDateTime": end.astimezone().isoformat(),
        "$select": "subject,start,end,location,isOnlineMeeting,onlineMeetingProvider,onlineMeeting,"
                   "isAllDay,isCancelled,showAs,attendees,organizer",
        "$orderby": "start/dateTime", "$top": "100",
    })
    request = urllib.request.Request(f"{GRAPH}/me/calendarView?{query}",
                                     headers={"Authorization": f"Bearer {token}", "Prefer": 'outlook.timezone="UTC"'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise IntegrationError(f"m365: Graph answered {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise IntegrationError(f"m365: Graph not reachable ({exc})") from exc
    return [to_event(item, me) for item in data.get("value", [])]


# --------------------------------------------------------------------------- the shared calendar


class CalendarSource:
    """The calendar of the next 24 hours, fetched every few minutes while a trigger uses it."""

    def __init__(self, fetch: Callable[[datetime, datetime], list[dict]] = fetch_graph,
                 clock: Callable[[], datetime] = datetime.now, poll: float = POLL_SECONDS):
        self.fetch = fetch
        self.clock = clock
        self.poll = poll
        self.events: list[dict] | None = None
        self.fetched: datetime | None = None
        self.error = ""
        self._users = 0
        self._task: asyncio.Task | None = None

    async def refresh(self) -> list[dict]:
        now = self.clock()
        try:
            events = await asyncio.to_thread(self.fetch, now - timedelta(hours=12), now + LOOKAHEAD)
        except IntegrationError as exc:
            self.error = str(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - reported in the bar
            self.error = f"m365: {exc}"
            raise IntegrationError(self.error) from exc
        self.events, self.fetched, self.error = events, now, ""
        return events

    async def current(self) -> list[dict]:
        """The events, fetched again when the last fetch is older than 10 minutes."""
        if self.events is None or self.fetched is None or self.clock() - self.fetched > FRESH:
            await self.refresh()
        return self.events or []

    def use(self) -> Callable[[], None]:
        self._users += 1
        if self._task is None:
            self._task = asyncio.create_task(self._poll())

        def release():
            self._users -= 1
            if self._users <= 0 and self._task:
                self._task.cancel()
                self._task = None
        return release

    async def _poll(self):
        while True:
            try:
                await self.refresh()
            except IntegrationError:
                pass  # The error stays in self.error; the last fetched calendar keeps counting.
            await asyncio.sleep(self.poll)

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


def calendar(services: Services) -> CalendarSource:
    if NAME not in services.sources:
        services.sources[NAME] = CalendarSource(clock=services.clock)
    return services.sources[NAME]


def matches(event: dict, options: dict) -> bool:
    if event["cancelled"] or event["all_day"] or not event["start"]:
        return False
    if event["show_as"] in options.get("skip_show_as", ["free", "oof"]):
        return False
    if options.get("online") and not event["online"]:
        return False
    if options.get("with_others") and event["others"] < 1:
        return False
    if options.get("subject") and options["subject"].lower() not in event["subject"].lower():
        return False
    return True


def outputs(event: dict) -> dict:
    return {
        "event_id": event["event_id"],
        "subject": event["subject"],
        "start": event["start"].strftime("%Y-%m-%d %H:%M"),
        "end": event["end"].strftime("%Y-%m-%d %H:%M") if event["end"] else "",
        "join_url": event["join_url"],
        "location": event["location"],
    }


# --------------------------------------------------------------------------- blocks


def _fired_file() -> Path:
    return paths.state_dir() / "calendar.json"


def _load_fired() -> dict:
    try:
        return json.loads(_fired_file().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class CalendarEvent(Trigger):
    type = "calendar_event"
    integration = NAME
    description = "A while before an item in the Outlook calendar starts"
    options = {
        "type": "object",
        "required": ["before"],
        "additionalProperties": False,
        "properties": {"before": {**DURATION_SCHEMA, "description": "How long before the start"}, **FILTER_OPTIONS},
    }
    outputs = {"event_id": "Graph id of the item", "subject": "subject", "start": "start (YYYY-MM-DD HH:MM)",
               "end": "end", "join_url": "Teams link", "location": "location"}

    def start(self, options, services, fire, key=""):
        source = calendar(services)
        release = source.use()
        before = timedelta(seconds=seconds(options["before"]))

        async def tick():
            while True:
                now = services.clock()
                fired = _load_fired()
                mine = fired.get(key, {})
                for event in source.events or []:
                    if not matches(event, options) or not (event["start"] - before <= now < event["start"]):
                        continue
                    start = event["start"].isoformat(timespec="minutes")
                    if mine.get(event["event_id"]) == start:
                        continue
                    # Record first, so a restart during the run does not fire it again.
                    mine[event["event_id"]] = start
                    fired[key] = {i: s for i, s in mine.items() if datetime.fromisoformat(s) > now - timedelta(days=1)}
                    write_atomic(_fired_file(), json.dumps(fired, indent=2, sort_keys=True) + "\n")
                    result = fire(outputs(event))
                    if asyncio.iscoroutine(result):
                        await result
                await asyncio.sleep(services.schedule_tick)

        task = asyncio.create_task(tick())

        def stop():
            task.cancel()
            release()
        return stop


class CalendarNow(Action):
    type = "calendar_now"
    integration = NAME
    description = "The calendar item happening now (or starting within 5 minutes); never fails"
    options = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"fallback": {"type": "string", "description": "subject when there is no item"}, **FILTER_OPTIONS},
    }
    outputs = {"subject": "subject, or the fallback", "found": "true when an item was found",
               "event_id": "Graph id", "start": "start", "error": "why the calendar could not be read"}
    retry_safe = True
    default_timeout = "30s"

    async def run(self, options, services, run_info=None):
        fallback = {"subject": options.get("fallback", ""), "found": False, "event_id": "", "start": "", "error": ""}
        try:
            events = await asyncio.wait_for(calendar(services).current(), 8)
        except (IntegrationError, asyncio.TimeoutError) as exc:
            return {**fallback, "error": str(exc) or "calendar timed out"}
        now = services.clock()
        live = [e for e in events if matches(e, options) and e["start"] - NOW_LEAD <= now < (e["end"] or e["start"])]
        if not live:
            return fallback
        event = min(live, key=lambda e: abs(e["start"] - now))
        out = outputs(event)
        return {"subject": out["subject"], "found": True, "event_id": out["event_id"], "start": out["start"], "error": ""}


TRIGGERS = [CalendarEvent()]
ACTIONS = [CalendarNow()]
