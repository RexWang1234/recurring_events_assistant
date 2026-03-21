#!/usr/bin/env python3
"""
Calendar Assistant

Commands:
  python main.py run             Start the Telegram bot + daily calendar scheduler
  python main.py status          Show status of all monitored events
  python main.py check           Check events and send Telegram alerts if due
  python main.py book <event>    Trigger AI booking agent for a named event (CLI, no Telegram)
"""

import sys
import yaml
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def cmd_run():
    from src.telegram_agent import run
    run()


def cmd_status():
    from src.calendar_client import get_event_status

    config = load_config()
    print("\n── Calendar Assistant Status ───────────────────────────\n")
    for ev in config["events"]:
        status = get_event_status(ev)
        last = status["last_occurrence"]
        next_due = status["next_due"]
        days = status["days_until_due"]
        next_sched = status["next_scheduled"]

        print(f"  Event         : {ev['name']}")
        if last:
            print(f"  Last done     : {last.strftime('%Y-%m-%d')}")
            print(f"  Next due      : {next_due.strftime('%Y-%m-%d')} ({days} days away)")
        else:
            print("  No past occurrences found in Apple Calendar.")
        if next_sched:
            print(f"  Already booked: {next_sched.strftime('%Y-%m-%d')} ✓")
        elif days is not None and days <= ev["alert_days_before"]:
            print(f"  ⚠  Not booked — within alert window ({ev['alert_days_before']}d)")
        print()


def cmd_check():
    """Run a one-off calendar check and send Telegram alerts if anything is due."""
    import asyncio
    import os
    from telegram import Bot
    from src.telegram_agent import check_calendar_and_alert

    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    async def _run():
        bot = Bot(token=token)
        await check_calendar_and_alert(bot, chat_id)

    asyncio.run(_run())
    print("Check complete.")


def cmd_book(event_name: str):
    from src.booking_agent import book_appointment

    config = load_config()
    user_info_path = BASE_DIR / "user_info.yaml"
    user_info = {}
    if user_info_path.exists():
        with open(user_info_path) as f:
            user_info = yaml.safe_load(f) or {}

    ev = next((e for e in config["events"] if e["name"].lower() == event_name.lower()), None)
    if ev is None:
        print(f"Error: No event named '{event_name}' found in config.yaml")
        print(f"Available events: {[e['name'] for e in config['events']]}")
        sys.exit(1)

    print(f"\n── Booking: {ev['name']} ────────────────────────────────\n")
    print(f"  URL         : {ev['booking_url']}")
    print(f"  Preferences : {ev.get('booking_preferences', '')}")
    print()

    confirmation = book_appointment(
        booking_url=ev["booking_url"],
        event_name=ev["name"],
        preferences=ev.get("booking_preferences", ""),
        user_info=user_info,
    )

    print("\n── Booking result ──────────────────────────────────────\n")
    print(confirmation)
    print()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "run":
        cmd_run()
    elif cmd == "status":
        cmd_status()
    elif cmd == "check":
        cmd_check()
    elif cmd == "book":
        if len(sys.argv) < 3:
            print("Usage: python main.py book <event-name>")
            sys.exit(1)
        cmd_book(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
