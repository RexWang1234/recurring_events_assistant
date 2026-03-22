"""
Telegram bot backed by a Claude AI agent.
Natural conversation replaces the old state machine — Claude decides what to say
and when to call tools (check calendar, fetch slots, book).
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot, Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from src.calendar_client import get_event_status
from src.booking_agent import get_available_slots, complete_booking, final_submit

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATE_PATH = BASE_DIR / "state.json"
USER_INFO_PATH = BASE_DIR / "user_info.yaml"

_anthropic = anthropic.Anthropic()
MAX_HISTORY = 40  # max messages kept in memory


# ── State helpers ──────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def _save_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _load_user_info() -> dict:
    if USER_INFO_PATH.exists():
        with open(USER_INFO_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


# ── Tools ──────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_calendar_status",
        "description": (
            "Check Apple Calendar for all configured recurring events. "
            "Returns each event's last occurrence, next due date, days until due, "
            "and whether a future appointment is already booked."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fetch_available_slots",
        "description": (
            "Browse the booking website to find available appointment slots for a specific event. "
            "This takes ~30–60 seconds as it actually opens the site."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_name": {
                    "type": "string",
                    "description": "Name of the event to book (e.g. 'Massage', 'Haircut')",
                },
            },
            "required": ["event_name"],
        },
    },
]


async def _execute_tool(name: str, inputs: dict, config: dict, user_info: dict) -> str:
    """Run a tool and return a string result for Claude."""
    if name == "get_calendar_status":
        lines = []
        for ev in config["events"]:
            status = get_event_status(ev)
            lines.append(
                f"{ev['name']}: last={status['last_occurrence']}, "
                f"next_due={status['next_due']}, days_until_due={status['days_until_due']}, "
                f"already_booked={status['next_scheduled']}"
            )
        return "\n".join(lines) if lines else "No events configured."

    elif name == "fetch_available_slots":
        event_name = inputs["event_name"]
        ev_config = next(
            (e for e in config["events"] if e["name"].lower() == event_name.lower()), None
        )
        if ev_config is None:
            return f"Unknown event: {event_name}"
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: get_available_slots(
                booking_url=ev_config["booking_url"],
                event_name=event_name,
                preferences=ev_config.get("booking_preferences", ""),
            ),
        )
        slots = result.get("slots", [])
        note = result.get("message", "")
        if not slots:
            return f"No available slots found. {note}".strip()
        slot_list = "\n".join(f"{i+1}. {s}" for i, s in enumerate(slots))
        return f"Available slots:\n{slot_list}" + (f"\n\n{note}" if note else "")

    elif name == "prepare_booking":
        event_name = inputs["event_name"]
        chosen_slot = inputs["chosen_slot"]
        ev_config = next(
            (e for e in config["events"] if e["name"].lower() == event_name.lower()), None
        )
        if ev_config is None:
            return f"Unknown event: {event_name}"
        summary = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: complete_booking(
                booking_url=ev_config["booking_url"],
                event_name=event_name,
                preferences=ev_config.get("booking_preferences", ""),
                chosen_slot=chosen_slot,
                user_info=user_info,
            ),
        )
        return summary[:1000]

    elif name == "submit_booking":
        event_name = inputs["event_name"]
        chosen_slot = inputs["chosen_slot"]
        ev_config = next(
            (e for e in config["events"] if e["name"].lower() == event_name.lower()), None
        )
        if ev_config is None:
            return f"Unknown event: {event_name}"
        confirmation = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: final_submit(
                booking_url=ev_config["booking_url"],
                event_name=event_name,
                chosen_slot=chosen_slot,
                user_info=user_info,
            ),
        )
        return confirmation[:600]

    return f"Unknown tool: {name}"


def _build_system_prompt(config: dict, user_info: dict) -> str:
    today = datetime.now().strftime("%A, %B %-d, %Y")
    events_desc = "\n".join(
        f"- {e['name']}: every {e['frequency_weeks']} week(s), "
        f"alert {e['alert_days_before']} days before due"
        for e in config.get("events", [])
    )
    user_desc = ""
    if user_info:
        user_desc = "\nUser info:\n" + "\n".join(f"  {k}: {v}" for k, v in user_info.items())
    return f"""You are a friendly personal calendar assistant running on the user's MacBook.

Today is {today}.

Configured recurring events:
{events_desc}{user_desc}

Your job:
- Proactively check the calendar and alert the user when appointments are coming due.
- Help them find available slots using your tools.
- Be warm, concise, and conversational — not robotic or menu-driven.
- When presenting slots, just list them naturally. Let the user reply however feels natural.
- Booking is not available yet — if the user wants to book, tell them to book manually for now and share the booking URL from the event config.
- If the user chats casually, respond naturally."""


def _serialize_content(content) -> list:
    """Convert SDK content blocks to plain dicts for JSON storage."""
    result = []
    for block in content:
        if hasattr(block, "model_dump"):
            result.append(block.model_dump())
        elif isinstance(block, dict):
            result.append(block)
        else:
            result.append({"type": "text", "text": str(block)})
    return result


# ── AI Agent loop ──────────────────────────────────────────────────────────────

async def run_agent(bot: Bot, chat_id: str, trigger_message: str):
    """
    Run one turn of the Claude agent. Claude may call multiple tools before
    producing a final reply. Conversation history is persisted across turns.
    """
    config = _load_config()
    user_info = _load_user_info()
    state = _load_state()
    history: list = state.get("_conversation_history", [])

    history.append({"role": "user", "content": trigger_message})
    system = _build_system_prompt(config, user_info)

    while True:
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _anthropic.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=system,
                tools=TOOLS,
                messages=history,
            ),
        )

        # Log token usage and cost (Haiku: $0.80/M input, $4.00/M output)
        in_tok = response.usage.input_tokens
        out_tok = response.usage.output_tokens
        cost = (in_tok * 0.80 + out_tok * 4.00) / 1_000_000
        tool_names = [b.name for b in response.content if b.type == "tool_use"]
        step = ", ".join(tool_names) if tool_names else "reply"
        logger.info(f"[tokens] in={in_tok} out={out_tok} cost=${cost:.5f} | {step}")

        text_parts = [b.text for b in response.content if b.type == "text"]
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        # Send any text response to Telegram
        if text_parts:
            await bot.send_message(chat_id=chat_id, text="\n".join(text_parts))

        # Persist assistant turn
        history.append({"role": "assistant", "content": _serialize_content(response.content)})

        if not tool_uses:
            break

        # Execute tools, notifying for slow ones
        tool_results = []
        for tu in tool_uses:
            if tu.name in ("fetch_available_slots", "prepare_booking", "submit_booking"):
                await bot.send_message(chat_id=chat_id, text="_(on it, give me a moment...)_", parse_mode="Markdown")
            try:
                result = await _execute_tool(tu.name, tu.input, config, user_info)
            except Exception as e:
                logger.exception(f"Tool {tu.name} failed")
                result = f"Error running {tu.name}: {e}"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result,
            })

        history.append({"role": "user", "content": tool_results})

    # Trim and persist history
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    state["_conversation_history"] = history
    _save_state(state)


# ── Telegram handlers ──────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    text = (update.message.text or "").strip()
    try:
        await run_agent(context.bot, chat_id, text)
    except Exception as e:
        logger.exception("run_agent failed")
        await update.message.reply_text(f"⚠️ Error: {e}")


# ── Entry point ────────────────────────────────────────────────────────────────

def run():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    scheduler = AsyncIOScheduler()

    async def post_init(application):
        async def scheduled_check():
            await run_agent(
                application.bot,
                chat_id,
                "Please check my calendar now and let me know if any recurring appointments "
                "are coming up that I haven't booked yet.",
            )

        scheduler.add_job(scheduled_check, "interval", hours=24, next_run_time=datetime.now())
        scheduler.start()
        logger.info("Scheduler started.")

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Calendar Assistant starting. Listening for Telegram messages...")
    app.run_polling(allowed_updates=["message"])
