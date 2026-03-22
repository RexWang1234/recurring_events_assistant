# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Start the bot (Telegram + daily scheduler)
python3 main.py run

# Check calendar status in terminal (no Telegram)
python3 main.py status

# Test availability scraping for a specific event
python3 -c "
from src.booking_agent import get_available_slots
print(get_available_slots('https://...', 'Massage', 'weekday afternoon'))
"

# Discover a new site's API (run headless=False browser, captures all JSON calls)
python3 scripts/discover_jane_api.py
python3 scripts/discover_booksy_api.py

# Install deps
pip install -r requirements.txt
playwright install chromium
```

There are no tests. `python3 main.py status` is the primary way to verify calendar parsing is working.

## Environment

Requires a `.env` file with:
- `ANTHROPIC_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

`main.py` calls `load_dotenv()` at startup. Scrapers called directly from the REPL need env vars loaded manually.

## Architecture

The bot is a **Claude Haiku agent loop** — not a state machine. Each incoming Telegram message (or scheduled trigger) runs `run_agent()`, which calls the Anthropic API in a loop until `stop_reason == "end_turn"`, executing tools along the way.

### Request flow

```
Telegram message / APScheduler tick
  → run_agent(bot, chat_id, trigger_message)
      → Claude: may call get_calendar_status and/or fetch_available_slots
      → _execute_tool() dispatches each tool
      → fetch_available_slots → booking_agent.get_available_slots()
                                    → jane_app_scraper   (janeapp.com)
                                    → booksy_scraper     (booksy.com)
                                    → generic_api_sniffer (everything else)
      → Claude produces final reply → bot.send_message()
```

Conversation history is stored in `state.json` as `_conversation_history` and trimmed to `MAX_HISTORY=40` messages. All interactions are appended to `conversation_log.jsonl`.

### Scraper pattern

All three scrapers use **Playwright network interception** — load the page, click the right element, capture JSON API responses. No screenshot parsing, no CSS selector guessing.

- **`jane_app_scraper.py`**: intercepts `/api/v2/openings/for_discipline`. Service to click is inferred from event name/preferences via `_service_keywords()`. Also intercepts `staff_members` URLs to map IDs to names.
- **`booksy_scraper.py`**: intercepts `/appointments/time_slots`. Schema: `{"time_slots": [{"date": "YYYY-MM-DD", "slots": [{"t": "HH:MM", "p": ""}]}]}`. Filters by day/time preference before returning slots.
- **`generic_api_sniffer.py`**: captures all JSON API responses, tries a service-specific click then generic triggers, sends captured data to Claude Haiku to identify and format slots.

### Adding a new booking site

1. Run a discovery script (copy `scripts/discover_jane_api.py`, point it at the new URL, run with `headless=False`)
2. Find the availability endpoint in the captured JSON
3. Add a scraper in `src/` following the Playwright interception pattern
4. Register it in `booking_agent.get_available_slots()` with a domain check

### Scheduler

APScheduler `AsyncIOScheduler` must be started inside `post_init(application)` — not at module level — to avoid "no current event loop" errors with `python-telegram-bot`'s async setup.

### Config-driven events

`config.yaml` drives everything. Each event needs:
- `name`, `frequency_weeks`, `alert_days_before`, `booking_url`, `booking_preferences`
- `calendar_search_keywords` — list of strings matched (case-insensitive substring) against Apple Calendar event titles

Apple Calendar is read via `osascript` AppleScript. Calendars named "Canadian Holidays", "Birthdays", "Siri Suggestions", "Scheduled Reminders" are skipped.
