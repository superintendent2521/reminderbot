# Reminder Bot

Discord reminder bot built with `interactions.py` and a JSON file for storage.

## Features

- One-time reminders with `YYYY-MM-DD HH:MM`
- Repeating weekday reminders with `HH:MM`
- End by duration in days, by explicit end date, or keep repeating forever
- Delivery by DM, current channel ping, or chosen channel ping
- Per-user timezone, defaulting to `America/New_York`
- JSON persistence in `data/reminders.json`
- Slash command management plus a menu/button management panel

## Setup

1. Install Python 3.11+.
2. Install dependencies:
   ```bash
   python3 -m pip install -r requirements.txt
   ```
3. Set your bot token:
   ```bash
   export DISCORD_BOT_TOKEN="your-token"
   ```
4. Run the bot:
   ```bash
   python3 bot.py
   ```

## Commands

- `/timezone [timezone_name]`
- `/reminder create`
- `/reminder list`
- `/reminder manage`
- `/reminder edit reminder_id:<id> [time_input] [message] [delivery] [weekdays] [duration_days] [end_date] [target_channel]`
- `/reminder pause reminder_id:<id>`
- `/reminder resume reminder_id:<id>`
- `/reminder delete reminder_id:<id>`

## Create command notes

- `schedule_type=once`: use `time_input` as `YYYY-MM-DD HH:MM`
- `schedule_type=weekly`: use `time_input` as `HH:MM` and `weekdays` like `mon,wed,fri`
- For repeating reminders, use either `duration_days`, `end_date`, or neither for forever
- `target_channel` is only used when `delivery=channel`

## Timezones

Use IANA names like:

- `America/New_York`
- `America/Chicago`
- `Europe/London`
- `Asia/Tokyo`
