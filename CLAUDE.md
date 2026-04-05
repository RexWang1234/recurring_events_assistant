# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Start the bot (Telegram + daily scheduler)
python3 main.py run

# Check calendar status in terminal (no Telegram)
python3 main.py status

# Test availability scraping
python3 -c "
from dotenv import load_dotenv; load_dotenv()
from src.slot_service import fetch_and_filter_slots
result = fetch_and_filter_slots('https://mobilitypluschiropractic.janeapp.com/', 'Massage', 'weekday afternoon', 'Mobility Plus')
for s in result['slots']: print(s.to_display())
"

# Discover a new site's API (captures all JSON calls)
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

`main.py` calls `load_dotenv()` at startup.

## Architecture

### Layered design

```
telegram_agent.py  — Claude agent loop (intent + reply generation ONLY)
  → slot_service.py  — orchestration: fetch → filter → rank (deterministic)
      → scrapers      — platform-specific API interception (no AI)
      → preferences.py — "weekday afternoon" → structured filters
      → models.py     — normalized Slot dataclass
  → db.py            — SQLite for conversation history + logs
  → calendar_client.py — Apple Calendar via osascript
```

**Claude handles conversation, not data.** All slot extraction, filtering, and ranking is deterministic Python. Claude is only used for intent parsing and reply generation (and as a last resort in the generic sniffer for unknown API schemas).

### Normalized slot schema (models.py)

All scrapers return `list[Slot]` with fields: `shop`, `service`, `provider`, `start_time`, `end_time`, `duration_min`, `source_url`, `platform`.

### Scraper interface

Each scraper exports: `scrape_<platform>_slots(url, event_name, preferences, shop_name) -> list[Slot]`

- **`jane_app_scraper.py`**: intercepts `/api/v2/openings/for_discipline` + `staff_members`
- **`booksy_scraper.py`**: intercepts `/appointments/time_slots`
- **`generic_api_sniffer.py`**: captures all JSON APIs, uses Claude to parse (fallback only)

### Adding a new booking site

1. Run a discovery script (copy `scripts/discover_jane_api.py`, point at new URL)
2. Find the availability endpoint in captured JSON
3. Add `src/<platform>_scraper.py` implementing `scrape_<platform>_slots() -> list[Slot]`
4. Register in `slot_service.py` `_fetch_raw_slots()` with a domain check

### State management

- **SQLite** (`calendar_assistant.db`): conversation history, event logs
- **config.yaml**: event definitions, shop metadata, preferences (source of truth)
- Old `state.json` / `conversation_log.jsonl` are no longer used

### Scheduler

APScheduler `AsyncIOScheduler` must start inside `post_init(application)` — not at module level — to avoid "no current event loop" errors.

### Config-driven events

`config.yaml` drives everything. Each event needs:
- `name`, `shop_name`, `frequency_weeks`, `alert_days_before`, `booking_url`, `booking_preferences`
- `calendar_search_keywords` — list of strings matched (case-insensitive substring) against Apple Calendar event titles

Apple Calendar is read via `osascript`. Calendars named "Canadian Holidays", "Birthdays", "Siri Suggestions", "Scheduled Reminders" are skipped.
