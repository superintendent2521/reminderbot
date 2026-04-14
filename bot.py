import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import interactions
from interactions import (
    Button,
    ButtonStyle,
    ChannelType,
    ComponentContext,
    Intents,
    Modal,
    ModalContext,
    OptionType,
    ParagraphText,
    SlashCommandChoice,
    SlashContext,
    StringSelectMenu,
    StringSelectOption,
    component_callback,
    listen,
    modal_callback,
    slash_command,
    slash_option,
    spread_to_rows,
)

DATA_FILE = Path("data/reminders.json")
DEFAULT_TIMEZONE = "America/New_York"
CHECK_INTERVAL_SECONDS = 30
MANAGE_SELECT_PREFIX = "reminder_manage_select:"
MANAGE_ACTION_PREFIX = "reminder_manage_action:"
MANAGE_SELECT_PATTERN = re.compile(r"reminder_manage_select:([0-9]+)")
MANAGE_ACTION_PATTERN = re.compile(r"reminder_manage_action:([a-z_]+):([0-9]+):([0-9a-f-]+)")
EDIT_MODAL_PATTERN = re.compile(r"reminder_edit_modal:([0-9]+):([0-9a-f-]+)")

WEEKDAY_ALIASES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class Reminder:
    reminder_id: str
    user_id: int
    timezone: str
    message: str
    delivery: str
    guild_id: int | None
    channel_id: int | None
    schedule_type: str
    target_datetime: str | None
    reminder_time: str | None
    weekdays: list[int]
    end_at: str | None
    repeats_forever: bool
    active: bool
    last_sent_at: str | None
    next_trigger_at: str | None
    created_at: str


class Messageable(Protocol):
    async def send(self, content: str) -> Any:
        ...


class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self.data = self._load_sync()

    def _default_data(self) -> dict[str, Any]:
        return {"user_settings": {}, "reminders": []}

    def _load_sync(self) -> dict[str, Any]:
        if not self.path.exists():
            data = self._default_data()
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return data
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logging.warning("Reminder database is invalid JSON. Recreating empty store.")
            data = self._default_data()
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return data

    async def save(self) -> None:
        async with self._lock:
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def get_user_timezone(self, user_id: int) -> str:
        return self.data["user_settings"].get(str(user_id), {}).get("timezone", DEFAULT_TIMEZONE)

    async def set_user_timezone(self, user_id: int, tz_name: str) -> None:
        settings = self.data["user_settings"].setdefault(str(user_id), {})
        settings["timezone"] = tz_name
        await self.save()

    def all_reminders(self) -> list[Reminder]:
        return [Reminder(**item) for item in self.data["reminders"]]

    def get_user_reminders(self, user_id: int) -> list[Reminder]:
        reminders = [Reminder(**item) for item in self.data["reminders"] if item["user_id"] == user_id]
        reminders.sort(key=lambda item: item.next_trigger_at or "9999-12-31T23:59:59+00:00")
        return reminders

    def get_reminder(self, reminder_id: str) -> Reminder | None:
        for item in self.data["reminders"]:
            if item["reminder_id"] == reminder_id:
                return Reminder(**item)
        return None

    async def upsert_reminder(self, reminder: Reminder) -> None:
        for idx, item in enumerate(self.data["reminders"]):
            if item["reminder_id"] == reminder.reminder_id:
                self.data["reminders"][idx] = asdict(reminder)
                await self.save()
                return
        self.data["reminders"].append(asdict(reminder))
        await self.save()

    async def delete_reminder(self, reminder_id: str) -> bool:
        original = len(self.data["reminders"])
        self.data["reminders"] = [item for item in self.data["reminders"] if item["reminder_id"] != reminder_id]
        changed = len(self.data["reminders"]) != original
        if changed:
            await self.save()
        return changed


store = JsonStore(DATA_FILE)
bot = interactions.Client(intents=Intents.DEFAULT | Intents.GUILD_MESSAGES | Intents.DIRECT_MESSAGES)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def required_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def resolve_timezone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {tz_name}") from exc


def parse_date_time_input(value: str, tz_name: str) -> datetime:
    try:
        naive = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("Use `YYYY-MM-DD HH:MM` in 24-hour time.") from exc
    return naive.replace(tzinfo=resolve_timezone(tz_name))


def parse_time_input(value: str) -> str:
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M")
    except ValueError as exc:
        raise ValueError("Use `HH:MM` in 24-hour time.") from exc
    return parsed.strftime("%H:%M")


def parse_weekdays(value: str) -> list[int]:
    parts = [part.strip().lower() for part in value.split(",") if part.strip()]
    if not parts:
        raise ValueError("Provide at least one weekday, for example `mon,wed,fri`.")
    result: list[int] = []
    for part in parts:
        if part not in WEEKDAY_ALIASES:
            raise ValueError(f"Unknown weekday `{part}`. Use values like `mon,tue,fri`.")
        day = WEEKDAY_ALIASES[part]
        if day not in result:
            result.append(day)
    return sorted(result)


def parse_end_inputs(tz_name: str, duration_days: int | None, end_date: str | None) -> tuple[datetime | None, bool]:
    if duration_days is not None and end_date:
        raise ValueError("Use either `duration_days` or `end_date`, not both.")
    if duration_days is not None:
        if duration_days <= 0:
            raise ValueError("`duration_days` must be greater than 0.")
        return datetime.now(resolve_timezone(tz_name)) + timedelta(days=duration_days), False
    if end_date:
        return parse_date_time_input(end_date, tz_name), False
    return None, True


def compute_next_trigger(reminder: Reminder, now_utc: datetime | None = None) -> datetime | None:
    now_utc = now_utc or utc_now()
    tz = resolve_timezone(reminder.timezone)
    end_at = parse_iso(reminder.end_at)

    if reminder.schedule_type == "once":
        target = parse_iso(reminder.target_datetime)
        if not target:
            return None
        if end_at and target > end_at:
            return None
        if reminder.last_sent_at:
            return None
        return target if target > now_utc else None

    if reminder.schedule_type != "weekly":
        return None

    if not reminder.reminder_time or not reminder.weekdays:
        return None

    hour, minute = [int(part) for part in reminder.reminder_time.split(":", 1)]
    local_now = now_utc.astimezone(tz)
    for offset in range(0, 15):
        candidate_date = (local_now + timedelta(days=offset)).date()
        if candidate_date.weekday() not in reminder.weekdays:
            continue
        candidate_local = datetime(
            year=candidate_date.year,
            month=candidate_date.month,
            day=candidate_date.day,
            hour=hour,
            minute=minute,
            tzinfo=tz,
        )
        candidate_utc = candidate_local.astimezone(timezone.utc)
        if candidate_utc <= now_utc:
            continue
        if end_at and candidate_utc > end_at:
            return None
        return candidate_utc
    return None


def delivery_label(reminder: Reminder) -> str:
    if reminder.delivery == "dm":
        return "DM"
    if reminder.delivery == "channel" and reminder.channel_id:
        return f"Channel <#{reminder.channel_id}>"
    return "Current channel ping"


def schedule_label(reminder: Reminder) -> str:
    if reminder.schedule_type == "once":
        target = parse_iso(reminder.target_datetime)
        return f"One-time at {target.astimezone(resolve_timezone(reminder.timezone)).strftime('%Y-%m-%d %H:%M %Z')}" if target else "One-time"
    weekdays = ", ".join(WEEKDAY_NAMES[idx] for idx in reminder.weekdays)
    end_at = parse_iso(reminder.end_at)
    if reminder.repeats_forever or end_at is None:
        ending = "forever"
    else:
        ending = f"until {end_at.astimezone(resolve_timezone(reminder.timezone)).strftime('%Y-%m-%d %H:%M %Z')}"
    return f"Weekly on {weekdays} at {reminder.reminder_time} {resolve_timezone(reminder.timezone).key} ({ending})"


def reminder_summary(reminder: Reminder) -> str:
    next_trigger = parse_iso(reminder.next_trigger_at)
    next_text = next_trigger.astimezone(resolve_timezone(reminder.timezone)).strftime("%Y-%m-%d %H:%M %Z") if next_trigger else "No future trigger"
    status = "active" if reminder.active else "paused"
    return (
        f"`{reminder.reminder_id[:8]}` [{status}] {schedule_label(reminder)} | {delivery_label(reminder)} | next: {next_text}\n"
        f"Message: {reminder.message}"
    )


def build_manage_components(user_id: int, reminders: list[Reminder], selected_id: str | None = None) -> list[Any]:
    if not reminders:
        return []
    options = []
    for reminder in reminders[:25]:
        next_trigger = parse_iso(reminder.next_trigger_at)
        description = next_trigger.astimezone(resolve_timezone(reminder.timezone)).strftime("Next %Y-%m-%d %H:%M") if next_trigger else "No future trigger"
        options.append(
            StringSelectOption(
                label=f"{reminder.reminder_id[:8]} | {'active' if reminder.active else 'paused'}",
                value=reminder.reminder_id,
                description=description[:100],
                default=reminder.reminder_id == selected_id,
            )
        )
    select = StringSelectMenu(
        *options,
        custom_id=f"{MANAGE_SELECT_PREFIX}{user_id}",
        placeholder="Select a reminder",
        min_values=1,
        max_values=1,
    )
    target_id = selected_id or reminders[0].reminder_id
    buttons = [
        Button(style=ButtonStyle.PRIMARY, label="Refresh", custom_id=f"{MANAGE_ACTION_PREFIX}refresh:{user_id}:{target_id}"),
        Button(style=ButtonStyle.SECONDARY, label="Pause", custom_id=f"{MANAGE_ACTION_PREFIX}pause:{user_id}:{target_id}"),
        Button(style=ButtonStyle.SUCCESS, label="Resume", custom_id=f"{MANAGE_ACTION_PREFIX}resume:{user_id}:{target_id}"),
        Button(style=ButtonStyle.DANGER, label="Delete", custom_id=f"{MANAGE_ACTION_PREFIX}delete:{user_id}:{target_id}"),
        Button(style=ButtonStyle.PRIMARY, label="Edit Message", custom_id=f"{MANAGE_ACTION_PREFIX}edit:{user_id}:{target_id}"),
    ]
    return spread_to_rows(select, *buttons)


async def refresh_next_trigger(reminder: Reminder) -> Reminder:
    reminder.next_trigger_at = format_iso(compute_next_trigger(reminder))
    await store.upsert_reminder(reminder)
    return reminder


async def send_due_reminder(reminder: Reminder) -> None:
    if reminder.delivery == "dm":
        user = await bot.fetch_user(reminder.user_id)
        if user is None:
            raise ValueError(f"User {reminder.user_id} could not be fetched")
        dm_target = cast(Messageable, user)
        await dm_target.send(f"Reminder: {reminder.message}")
    else:
        if not reminder.channel_id:
            raise ValueError("Missing channel for non-DM reminder")
        channel = await bot.fetch_channel(reminder.channel_id)
        if channel is None:
            raise ValueError(f"Channel {reminder.channel_id} could not be fetched")
        channel_target = cast(Messageable, channel)
        await channel_target.send(f"<@{reminder.user_id}> Reminder: {reminder.message}")


async def reminder_loop() -> None:
    await bot.wait_until_ready()
    while True:
        now = utc_now()
        reminders = store.all_reminders()
        for reminder in reminders:
            if not reminder.active:
                continue
            next_trigger = parse_iso(reminder.next_trigger_at)
            if not next_trigger:
                continue
            if next_trigger > now:
                continue
            try:
                await send_due_reminder(reminder)
            except Exception:
                logging.exception("Failed to send reminder %s", reminder.reminder_id)
                continue
            reminder.last_sent_at = format_iso(now)
            if reminder.schedule_type == "once":
                reminder.active = False
                reminder.next_trigger_at = None
            else:
                reminder.next_trigger_at = format_iso(compute_next_trigger(reminder, now + timedelta(seconds=1)))
                if reminder.next_trigger_at is None:
                    reminder.active = False
            await store.upsert_reminder(reminder)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


@listen()
async def on_startup() -> None:
    logging.info("Bot startup complete.")
    for reminder in store.all_reminders():
        computed = compute_next_trigger(reminder)
        if format_iso(computed) != reminder.next_trigger_at:
            reminder.next_trigger_at = format_iso(computed)
            await store.upsert_reminder(reminder)
    asyncio.create_task(reminder_loop())


@slash_command(name="timezone", description="Set or view your default timezone")
@slash_option(
    name="timezone_name",
    description="IANA timezone like America/New_York",
    required=False,
    opt_type=OptionType.STRING,
)
async def timezone_command(ctx: SlashContext, timezone_name: str | None = None) -> None:
    user_id = int(ctx.author.id)
    if not timezone_name:
        current = store.get_user_timezone(user_id)
        await ctx.send(f"Your timezone is `{current}`.", ephemeral=True)
        return
    try:
        resolve_timezone(timezone_name)
    except ValueError as exc:
        await ctx.send(str(exc), ephemeral=True)
        return
    await store.set_user_timezone(user_id, timezone_name)
    await ctx.send(f"Default timezone updated to `{timezone_name}`.", ephemeral=True)


@slash_command(name="reminder", description="Reminder commands")
async def reminder_base(ctx: SlashContext) -> None:
    await ctx.send("Use a reminder subcommand.", ephemeral=True)


@reminder_base.subcommand(sub_cmd_name="create", sub_cmd_description="Create a reminder")
@slash_option(
    name="schedule_type",
    description="One-time or repeating reminder",
    required=True,
    opt_type=OptionType.STRING,
    choices=[
        SlashCommandChoice(name="One-time", value="once"),
        SlashCommandChoice(name="Repeating weekdays", value="weekly"),
    ],
)
@slash_option(
    name="time_input",
    description="YYYY-MM-DD HH:MM for one-time, HH:MM for repeating",
    required=True,
    opt_type=OptionType.STRING,
)
@slash_option(
    name="message",
    description="Reminder message",
    required=True,
    opt_type=OptionType.STRING,
    max_length=500,
)
@slash_option(
    name="delivery",
    description="Where the reminder should go",
    required=True,
    opt_type=OptionType.STRING,
    choices=[
        SlashCommandChoice(name="DM me", value="dm"),
        SlashCommandChoice(name="Ping me in this channel", value="current_channel"),
        SlashCommandChoice(name="Ping me in a chosen channel", value="channel"),
    ],
)
@slash_option(
    name="weekdays",
    description="For repeating reminders, like mon,wed,fri",
    required=False,
    opt_type=OptionType.STRING,
)
@slash_option(
    name="duration_days",
    description="For repeating reminders, how many days to keep it active",
    required=False,
    opt_type=OptionType.INTEGER,
)
@slash_option(
    name="end_date",
    description="For repeating reminders, end date in YYYY-MM-DD HH:MM",
    required=False,
    opt_type=OptionType.STRING,
)
@slash_option(
    name="target_channel",
    description="Chosen server channel when delivery is channel",
    required=False,
    opt_type=OptionType.CHANNEL,
    channel_types=[ChannelType.GUILD_TEXT],
)
async def reminder_create(
    ctx: SlashContext,
    schedule_type: str,
    time_input: str,
    message: str,
    delivery: str,
    weekdays: str | None = None,
    duration_days: int | None = None,
    end_date: str | None = None,
    target_channel: interactions.GuildText | None = None,
) -> None:
    user_id = int(ctx.author.id)
    tz_name = store.get_user_timezone(user_id)

    if delivery == "channel":
        if ctx.guild is None:
            await ctx.send("Chosen-channel reminders can only be created in a server.", ephemeral=True)
            return
        if target_channel is None:
            await ctx.send("Pick `target_channel` when using `delivery=channel`.", ephemeral=True)
            return
        channel_id = int(target_channel.id)
    elif delivery == "current_channel":
        if ctx.channel is None:
            await ctx.send("Current-channel reminders need a valid channel context.", ephemeral=True)
            return
        channel_id = int(ctx.channel.id)
    else:
        channel_id = None

    if delivery != "dm" and ctx.guild is None:
        await ctx.send("Server pings are only available inside a server.", ephemeral=True)
        return

    try:
        if schedule_type == "once":
            target_local = parse_date_time_input(time_input, tz_name)
            if target_local.astimezone(timezone.utc) <= utc_now():
                raise ValueError("One-time reminders must be in the future.")
            parsed_weekdays: list[int] = []
            end_at = None
            repeats_forever = False
            reminder_time = None
            target_datetime = format_iso(target_local)
        else:
            reminder_time = parse_time_input(time_input)
            parsed_weekdays = parse_weekdays(weekdays or "")
            end_local, repeats_forever = parse_end_inputs(tz_name, duration_days, end_date)
            target_datetime = None
            end_at = format_iso(end_local)
    except ValueError as exc:
        await ctx.send(str(exc), ephemeral=True)
        return

    reminder = Reminder(
        reminder_id=str(uuid.uuid4()),
        user_id=user_id,
        timezone=tz_name,
        message=message,
        delivery=delivery,
        guild_id=int(ctx.guild.id) if ctx.guild else None,
        channel_id=channel_id,
        schedule_type=schedule_type,
        target_datetime=target_datetime,
        reminder_time=reminder_time,
        weekdays=parsed_weekdays,
        end_at=end_at,
        repeats_forever=repeats_forever,
        active=True,
        last_sent_at=None,
        next_trigger_at=None,
        created_at=required_iso(utc_now()),
    )
    reminder.next_trigger_at = format_iso(compute_next_trigger(reminder))
    if reminder.next_trigger_at is None:
        await ctx.send("This schedule has no future trigger. Check the time, weekdays, and end date.", ephemeral=True)
        return
    await store.upsert_reminder(reminder)
    await ctx.send(
        f"Reminder created.\n{reminder_summary(reminder)}\nTimezone: `{tz_name}`",
        ephemeral=True,
    )


@reminder_base.subcommand(sub_cmd_name="list", sub_cmd_description="List your reminders")
async def reminder_list(ctx: SlashContext) -> None:
    reminders = store.get_user_reminders(int(ctx.author.id))
    if not reminders:
        await ctx.send("You have no reminders.", ephemeral=True)
        return
    body = "\n\n".join(reminder_summary(reminder) for reminder in reminders[:10])
    await ctx.send(body, ephemeral=True)


@reminder_base.subcommand(sub_cmd_name="manage", sub_cmd_description="Manage reminders with menus")
async def reminder_manage(ctx: SlashContext) -> None:
    reminders = store.get_user_reminders(int(ctx.author.id))
    if not reminders:
        await ctx.send("You have no reminders to manage.", ephemeral=True)
        return
    selected = reminders[0]
    await ctx.send(
        f"Selected reminder:\n{reminder_summary(selected)}",
        components=build_manage_components(int(ctx.author.id), reminders, selected.reminder_id),
        ephemeral=True,
    )


@component_callback(MANAGE_SELECT_PATTERN)
async def reminder_manage_select(ctx: ComponentContext) -> None:
    match = MANAGE_SELECT_PATTERN.fullmatch(ctx.custom_id)
    if not match:
        await ctx.send("Invalid selection payload.", ephemeral=True)
        return
    owner_id = int(match.group(1))
    if int(ctx.author.id) != owner_id:
        await ctx.send("You can only manage your own reminders.", ephemeral=True)
        return
    selected_id = ctx.values[0]
    reminder = store.get_reminder(selected_id)
    if reminder is None or reminder.user_id != owner_id:
        await ctx.send("That reminder no longer exists.", ephemeral=True)
        return
    reminders = store.get_user_reminders(owner_id)
    await ctx.edit_origin(
        content=f"Selected reminder:\n{reminder_summary(reminder)}",
        components=build_manage_components(owner_id, reminders, selected_id),
    )


@component_callback(MANAGE_ACTION_PATTERN)
async def reminder_manage_action(ctx: ComponentContext) -> None:
    match = MANAGE_ACTION_PATTERN.fullmatch(ctx.custom_id)
    if not match:
        await ctx.send("Invalid action payload.", ephemeral=True)
        return
    action, owner_id_text, reminder_id = match.groups()
    owner_id = int(owner_id_text)
    if int(ctx.author.id) != owner_id:
        await ctx.send("You can only manage your own reminders.", ephemeral=True)
        return

    reminder = store.get_reminder(reminder_id)
    if reminder is None or reminder.user_id != owner_id:
        await ctx.send("That reminder no longer exists.", ephemeral=True)
        return

    if action == "delete":
        await store.delete_reminder(reminder_id)
        reminders = store.get_user_reminders(owner_id)
        if not reminders:
            await ctx.edit_origin(content="Reminder deleted. No reminders left.", components=[])
            return
        selected = reminders[0]
        await ctx.edit_origin(
            content=f"Reminder deleted.\n\nSelected reminder:\n{reminder_summary(selected)}",
            components=build_manage_components(owner_id, reminders, selected.reminder_id),
        )
        return

    if action == "pause":
        reminder.active = False
        reminder.next_trigger_at = format_iso(compute_next_trigger(reminder))
        await store.upsert_reminder(reminder)
    elif action == "resume":
        reminder.active = True
        reminder.last_sent_at = None if reminder.schedule_type == "once" else reminder.last_sent_at
        reminder.next_trigger_at = format_iso(compute_next_trigger(reminder))
        await store.upsert_reminder(reminder)
    elif action == "refresh":
        await refresh_next_trigger(reminder)
    elif action == "edit":
        modal = Modal(
            ParagraphText(
                label="Reminder message",
                custom_id="message",
                value=reminder.message,
                max_length=500,
            ),
            title="Edit Reminder Message",
            custom_id=f"reminder_edit_modal:{owner_id}:{reminder_id}",
        )
        await ctx.send_modal(modal)
        return
    else:
        await ctx.send("Unknown action.", ephemeral=True)
        return

    updated = store.get_reminder(reminder_id)
    if updated is None:
        await ctx.edit_origin(content="That reminder no longer exists.", components=[])
        return
    reminders = store.get_user_reminders(owner_id)
    await ctx.edit_origin(
        content=f"Updated reminder:\n{reminder_summary(updated)}",
        components=build_manage_components(owner_id, reminders, reminder_id),
    )


@modal_callback(EDIT_MODAL_PATTERN)
async def reminder_edit_modal(ctx: ModalContext) -> None:
    match = EDIT_MODAL_PATTERN.fullmatch(ctx.custom_id)
    if not match:
        await ctx.send("Invalid modal payload.", ephemeral=True)
        return
    owner_id_text, reminder_id = match.groups()
    owner_id = int(owner_id_text)
    if int(ctx.author.id) != owner_id:
        await ctx.send("You can only edit your own reminders.", ephemeral=True)
        return
    reminder = store.get_reminder(reminder_id)
    if reminder is None or reminder.user_id != owner_id:
        await ctx.send("That reminder no longer exists.", ephemeral=True)
        return
    message = str(ctx.responses.get("message", "")).strip()
    if not message:
        await ctx.send("Reminder message cannot be empty.", ephemeral=True)
        return
    reminder.message = message
    await store.upsert_reminder(reminder)
    await ctx.send(f"Reminder updated.\n{reminder_summary(reminder)}", ephemeral=True)


@reminder_base.subcommand(sub_cmd_name="delete", sub_cmd_description="Delete a reminder by ID")
@slash_option(
    name="reminder_id",
    description="Reminder ID from /reminder list",
    required=True,
    opt_type=OptionType.STRING,
)
async def reminder_delete(ctx: SlashContext, reminder_id: str) -> None:
    reminder = store.get_reminder(reminder_id)
    if reminder is None or reminder.user_id != int(ctx.author.id):
        await ctx.send("Reminder not found.", ephemeral=True)
        return
    await store.delete_reminder(reminder_id)
    await ctx.send(f"Deleted reminder `{reminder_id}`.", ephemeral=True)


@reminder_base.subcommand(sub_cmd_name="edit", sub_cmd_description="Edit a reminder by ID")
@slash_option(
    name="reminder_id",
    description="Reminder ID from /reminder list",
    required=True,
    opt_type=OptionType.STRING,
)
@slash_option(
    name="time_input",
    description="New time. YYYY-MM-DD HH:MM for one-time, HH:MM for repeating",
    required=False,
    opt_type=OptionType.STRING,
)
@slash_option(
    name="message",
    description="New reminder message",
    required=False,
    opt_type=OptionType.STRING,
    max_length=500,
)
@slash_option(
    name="delivery",
    description="New delivery target",
    required=False,
    opt_type=OptionType.STRING,
    choices=[
        SlashCommandChoice(name="DM me", value="dm"),
        SlashCommandChoice(name="Ping me in this channel", value="current_channel"),
        SlashCommandChoice(name="Ping me in a chosen channel", value="channel"),
    ],
)
@slash_option(
    name="weekdays",
    description="For repeating reminders, like mon,wed,fri",
    required=False,
    opt_type=OptionType.STRING,
)
@slash_option(
    name="duration_days",
    description="For repeating reminders, how many days to keep it active",
    required=False,
    opt_type=OptionType.INTEGER,
)
@slash_option(
    name="end_date",
    description="For repeating reminders, end date in YYYY-MM-DD HH:MM",
    required=False,
    opt_type=OptionType.STRING,
)
@slash_option(
    name="target_channel",
    description="Chosen server channel when delivery is channel",
    required=False,
    opt_type=OptionType.CHANNEL,
    channel_types=[ChannelType.GUILD_TEXT],
)
async def reminder_edit(
    ctx: SlashContext,
    reminder_id: str,
    time_input: str | None = None,
    message: str | None = None,
    delivery: str | None = None,
    weekdays: str | None = None,
    duration_days: int | None = None,
    end_date: str | None = None,
    target_channel: interactions.GuildText | None = None,
) -> None:
    reminder = store.get_reminder(reminder_id)
    if reminder is None or reminder.user_id != int(ctx.author.id):
        await ctx.send("Reminder not found.", ephemeral=True)
        return

    tz_name = reminder.timezone
    if message is not None:
        reminder.message = message

    chosen_delivery = delivery or reminder.delivery
    if chosen_delivery == "dm":
        reminder.delivery = "dm"
        reminder.channel_id = None
    elif chosen_delivery == "current_channel":
        if ctx.guild is None or ctx.channel is None:
            await ctx.send("Current-channel delivery can only be set in a server channel.", ephemeral=True)
            return
        reminder.delivery = "current_channel"
        reminder.channel_id = int(ctx.channel.id)
        reminder.guild_id = int(ctx.guild.id)
    elif chosen_delivery == "channel":
        if ctx.guild is None:
            await ctx.send("Chosen-channel delivery can only be set in a server.", ephemeral=True)
            return
        if target_channel is None:
            await ctx.send("Pick `target_channel` when using `delivery=channel`.", ephemeral=True)
            return
        reminder.delivery = "channel"
        reminder.channel_id = int(target_channel.id)
        reminder.guild_id = int(ctx.guild.id)

    try:
        if reminder.schedule_type == "once":
            if weekdays is not None or duration_days is not None or end_date is not None:
                raise ValueError("One-time reminders only support `time_input`, `message`, and delivery changes.")
            if time_input is not None:
                target_local = parse_date_time_input(time_input, tz_name)
                if target_local.astimezone(timezone.utc) <= utc_now():
                    raise ValueError("One-time reminders must be in the future.")
                reminder.target_datetime = format_iso(target_local)
                reminder.last_sent_at = None
        else:
            if time_input is not None:
                reminder.reminder_time = parse_time_input(time_input)
            if weekdays is not None:
                reminder.weekdays = parse_weekdays(weekdays)
            if duration_days is not None or end_date is not None:
                end_local, repeats_forever = parse_end_inputs(tz_name, duration_days, end_date)
                reminder.end_at = format_iso(end_local)
                reminder.repeats_forever = repeats_forever
    except ValueError as exc:
        await ctx.send(str(exc), ephemeral=True)
        return

    reminder.next_trigger_at = format_iso(compute_next_trigger(reminder))
    if reminder.active and reminder.next_trigger_at is None:
        await ctx.send("The edited reminder has no future trigger. Check the new time, weekdays, or end date.", ephemeral=True)
        return

    await store.upsert_reminder(reminder)
    await ctx.send(f"Reminder updated.\n{reminder_summary(reminder)}", ephemeral=True)


@reminder_base.subcommand(sub_cmd_name="pause", sub_cmd_description="Pause a reminder by ID")
@slash_option(
    name="reminder_id",
    description="Reminder ID from /reminder list",
    required=True,
    opt_type=OptionType.STRING,
)
async def reminder_pause(ctx: SlashContext, reminder_id: str) -> None:
    reminder = store.get_reminder(reminder_id)
    if reminder is None or reminder.user_id != int(ctx.author.id):
        await ctx.send("Reminder not found.", ephemeral=True)
        return
    reminder.active = False
    await store.upsert_reminder(reminder)
    await ctx.send(f"Paused reminder `{reminder_id}`.", ephemeral=True)


@reminder_base.subcommand(sub_cmd_name="resume", sub_cmd_description="Resume a reminder by ID")
@slash_option(
    name="reminder_id",
    description="Reminder ID from /reminder list",
    required=True,
    opt_type=OptionType.STRING,
)
async def reminder_resume(ctx: SlashContext, reminder_id: str) -> None:
    reminder = store.get_reminder(reminder_id)
    if reminder is None or reminder.user_id != int(ctx.author.id):
        await ctx.send("Reminder not found.", ephemeral=True)
        return
    reminder.active = True
    reminder.next_trigger_at = format_iso(compute_next_trigger(reminder))
    await store.upsert_reminder(reminder)
    await ctx.send(f"Resumed reminder `{reminder_id}`.", ephemeral=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set DISCORD_BOT_TOKEN in the environment.")
    bot.start(token)


if __name__ == "__main__":
    main()
