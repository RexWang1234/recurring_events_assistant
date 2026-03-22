# Calendar Assistant

A personal AI assistant that monitors your recurring appointments and proactively alerts you via Telegram when it's time to book — then fetches real availability so you can act immediately.

## What it does

- **Tracks recurring events** (e.g. haircut every 4 weeks, massage every 2 weeks) against your Apple Calendar
- **Sends a daily Telegram check-in** — if something is overdue or coming up with no booking, it alerts you and fetches available slots
- **Checks availability automatically** using site-specific scrapers (no login required)
- **Conversational AI** — powered by Claude Haiku; chat naturally to ask about your schedule, check slots, or get booking links

## Architecture

```
Apple Calendar (osascript)
       │
       ▼
Claude Haiku agent loop  ◄──►  Telegram bot (python-telegram-bot)
       │
       ├── get_calendar_status   → reads Apple Calendar via AppleScript
       └── fetch_available_slots → routes to the right scraper:
               ├── janeapp.com   → Jane App API scraper (intercepts /api/v2/openings/for_discipline)
               ├── booksy.com    → Booksy API scraper (intercepts /appointments/time_slots)
               └── everything else → Generic API sniffer (captures JSON APIs, Claude parses)
```

**Conversation history** is persisted across restarts in `state.json`.
**All interactions** are logged to `conversation_log.jsonl` (timestamp, event type, tokens, cost).

## Scrapers

| Platform | Method | Notes |
|---|---|---|
| Jane App | Network interception | Intercepts `openings/for_discipline` API; supports any service type (massage, chiro, acupuncture, etc.) |
| Booksy | Network interception | Intercepts `appointments/time_slots` API; filters by day/time preference |
| Other | Generic API sniffer | Captures all JSON APIs during page load + first click; Claude identifies slot data |

All scrapers use **Playwright headless Chrome** — no login required, no screenshot-based AI, no fragile CSS selectors.

## Setup

### Requirements

```bash
pip install -r requirements.txt
playwright install chromium
```

### Configuration

1. Copy and fill in credentials:
   ```bash
   cp .env.example .env
   ```
   Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `ANTHROPIC_API_KEY`.

2. Edit `config.yaml` to add your recurring events:
   ```yaml
   events:
     - name: Haircut
       frequency_weeks: 4
       alert_days_before: 7
       booking_url: "https://booksy.com/..."
       booking_preferences: "Saturday morning, any stylist"
       calendar_search_keywords: ["haircut", "hair"]
   ```

3. Optionally create `user_info.yaml` with your name/contact details for form-filling.

### Run

```bash
python3 main.py run
```

The bot starts, runs an immediate calendar check, then checks every 24 hours automatically. Message it on Telegram anytime to ask about your schedule or check availability.

## Logging

Every conversation turn is appended to `conversation_log.jsonl`:

```json
{"ts": "...", "chat_id": "...", "event": "user_message", "content": "..."}
{"ts": "...", "chat_id": "...", "event": "llm_call", "tools_called": ["fetch_available_slots"], "in_tokens": 2757, "out_tokens": 71, "cost_usd": 0.000249}
{"ts": "...", "chat_id": "...", "event": "tool_result", "tool": "fetch_available_slots", "result_preview": "..."}
{"ts": "...", "chat_id": "...", "event": "assistant_reply", "content": "..."}
```

## Adding a new booking site

1. Run the discovery script to capture the site's API calls:
   ```bash
   python3 scripts/discover_jane_api.py   # or discover_booksy_api.py
   ```
2. Identify the availability endpoint in the captured JSON
3. Add a scraper in `src/` following the pattern of `jane_app_scraper.py`
4. Register it in `src/booking_agent.py` `get_available_slots()`
