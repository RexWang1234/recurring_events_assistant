# Calendar Assistant

A personal calendar assistant that monitors recurring appointments via Apple Calendar and alerts you on Telegram when it's time to book again. It automatically checks availability at your booking sites (Jane App, Booksy, and others) and presents the best matching slots based on your preferences.

## How it works

```
Telegram / Scheduled trigger
  -> Claude agent (intent parsing + reply generation)
      -> get_calendar_status (Apple Calendar via osascript)
      -> fetch_available_slots
            -> slot_service (orchestration, filtering, ranking)
                -> jane_app_scraper   (janeapp.com - API interception)
                -> booksy_scraper     (booksy.com - API interception)
                -> generic_api_sniffer (other sites - JSON capture + Claude parsing)
  -> Telegram reply with slots + booking link
```

**Key design principles:**
- **Claude handles conversation, not data.** Slot extraction, filtering, and ranking are deterministic Python. Claude only parses user intent and generates natural replies.
- **Platform-specific scrapers first.** Known platforms (Jane App, Booksy) use dedicated API interception for speed and reliability. Unknown sites fall back to a generic JSON sniffer.
- **Normalized slot schema.** All scrapers return the same `Slot` dataclass, making filtering and display consistent regardless of source.
- **Explicit shop setup.** Booking sites and preferences are configured in `config.yaml`, not auto-discovered.

## Quick start

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Configure .env with ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
# Edit config.yaml with your recurring events and booking URLs

# Run
python main.py run      # start Telegram bot + daily scheduler
python main.py status   # check calendar status in terminal
```

## Adding a new booking site

1. Add the event to `config.yaml` with `shop_name`, `booking_url`, and `booking_preferences`
2. If it's a known platform (Jane App, Booksy), it works automatically
3. For other sites, the generic sniffer will attempt to capture JSON APIs
4. To add a dedicated scraper: create `src/<platform>_scraper.py`, implement `scrape_<platform>_slots()` returning `list[Slot]`, register it in `src/slot_service.py`

## Architecture

| Layer | File | Role |
|-------|------|------|
| Entry point | `main.py` | CLI commands (`run`, `status`) |
| Agent | `src/telegram_agent.py` | Telegram bot + Claude agent loop |
| Orchestration | `src/slot_service.py` | Fetch -> filter -> rank slots |
| Preferences | `src/preferences.py` | Natural language -> structured filters |
| Models | `src/models.py` | `Slot` and `TimePreference` dataclasses |
| Scrapers | `src/jane_app_scraper.py` | Jane App API interception |
| | `src/booksy_scraper.py` | Booksy API interception |
| | `src/generic_api_sniffer.py` | Generic JSON capture (fallback) |
| Calendar | `src/calendar_client.py` | Apple Calendar via osascript |
| State | `src/db.py` | SQLite for conversation history + logs |
| Config | `config.yaml` | Event definitions and booking preferences |

## Logging

All conversation events are stored in SQLite (`calendar_assistant.db`). Token usage and cost are tracked per API call (Haiku: $0.80/M input, $4.00/M output).
