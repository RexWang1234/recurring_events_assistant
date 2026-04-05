#!/usr/bin/env python3
"""
Calendar Assistant

Commands:
  python main.py run             Start the Telegram bot + daily calendar scheduler
  python main.py status          Show status of all monitored events
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
    from src.slot_service import get_shops

    config = load_config()
    print("\n── Calendar Assistant Status ───────────────────────────\n")
    for ev in config["events"]:
        status = get_event_status(ev)
        last = status["last_occurrence"]
        next_due = status["next_due"]
        days = status["days_until_due"]
        next_sched = status["next_scheduled"]

        print(f"  Event         : {ev['name']}")
        shops = get_shops(ev)
        for s in shops:
            print(f"  Shop          : {s['name']} ({s['url'][:50]}...)")
        if last:
            print(f"  Last done     : {last.strftime('%Y-%m-%d')}")
            print(f"  Next due      : {next_due.strftime('%Y-%m-%d')} ({days} days away)")
        else:
            print("  No past occurrences found in Apple Calendar.")
        if next_sched:
            print(f"  Already booked: {next_sched.strftime('%Y-%m-%d')}")
        elif days is not None and days <= ev["alert_days_before"]:
            print(f"  !! Not booked -- within alert window ({ev['alert_days_before']}d)")
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
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
