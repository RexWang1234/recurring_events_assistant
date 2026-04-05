"""
Telegram bot backed by a Claude AI agent.

The agent handles intent parsing and reply generation.
All slot fetching, filtering, and ranking is done deterministically
by slot_service -- Claude never touches raw slot data.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot, Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from src.calendar_client import get_event_status
from src.booking_agent import get_available_slots
from src.slot_service import detect_platform
from src import db

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
USER_INFO_PATH = BASE_DIR / "user_info.yaml"

_anthropic = anthropic.Anthropic()
MAX_HISTORY = 40


# -- Config helpers ------------------------------------------------------------

def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _load_user_info() -> dict:
    if USER_INFO_PATH.exists():
        with open(USER_INFO_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


# -- Tools exposed to Claude ---------------------------------------------------

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
            "Check the booking website to find available appointment slots. "
            "Returns a pre-filtered, ranked list of slots matching user preferences. "
            "Takes ~30-60 seconds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_name": {
                    "type": "string",
                    "description": "Name of the event (e.g. 'Massage', 'Haircut')",
                },
            },
            "required": ["event_name"],
        },
    },
]


async def _execute_tool(name: str, inputs: dict, config: dict) -> str:
    """Run a tool and return a string result for Claude."""
    if name == "get_calendar_status":
        lines = []
        for ev in config["events"]:
            status = get_event_status(ev)
            lines.append(
                f"{ev['name']}: last={status['last_occurrence']}, "
                f"next_due={status['next_due']}, "
                f"days_until_due={status['days_until_due']}, "
                f"already_booked={status['next_scheduled']}"
            )
        return "\n".join(lines) if lines else "No events configured."

    elif name == "fetch_available_slots":
        event_name = inputs["event_name"]
        ev_config = next(
            (e for e in config["events"]
             if e["name"].lower() == event_name.lower()),
            None,
        )
        if ev_config is None:
            return f"Unknown event: {event_name}"

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: get_available_slots(
                booking_url=ev_config["booking_url"],
                event_name=event_name,
                preferences=ev_config.get("booking_preferences", ""),
                shop_name=ev_config.get("shop_name", ""),
            ),
        )
        return result["display"]

    return f"Unknown tool: {name}"


def _build_system_prompt(config: dict, user_info: dict) -> str:
    today = datetime.now().strftime("%A, %B %-d, %Y")
    events_desc = "\n".join(
        f"- {e['name']}: every {e['frequency_weeks']} week(s), "
        f"alert {e['alert_days_before']} days before due, "
        f"book at {e.get('shop_name', '')} ({e['booking_url']})"
        for e in config.get("events", [])
    )
    user_desc = ""
    if user_info:
        user_desc = "\nUser info:\n" + "\n".join(
            f"  {k}: {v}" for k, v in user_info.items()
        )
    return f"""You are a friendly personal calendar assistant running on the user's MacBook.

Today is {today}.

Configured recurring events:
{events_desc}{user_desc}

Your job:
- When checking the calendar (scheduled or on request): call get_calendar_status, \
then for EVERY event that is due soon and not yet booked, automatically call \
fetch_available_slots so the user gets the full picture in one message.
- When presenting slots, always include the booking link from the tool result \
so the user can book directly.
- Be warm, concise, and conversational -- not robotic or menu-driven.
- When listing slots, present them naturally (numbered list). Mention the \
clinic/site name and include the booking link.
- Booking submission is not available through the bot -- always direct the \
user to the booking link to complete it.
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


# -- AI Agent loop -------------------------------------------------------------

async def run_agent(bot: Bot, chat_id: str, trigger_message: str):
    """Run one turn of the Claude agent.

    Claude may call multiple tools before producing a final reply.
    Conversation history is persisted in SQLite across turns.
    """
    config = _load_config()
    user_info = _load_user_info()

    # Load history from SQLite
    history = db.get_history(chat_id, limit=MAX_HISTORY)
    history.append({"role": "user", "content": trigger_message})
    db.append_message(chat_id, "user", trigger_message)
    db.log_event(chat_id, "user_message", content=trigger_message)

    system = _build_system_prompt(config, user_info)

    while True:
        try:
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
        except Exception as e:
            if "400" in str(e) and (
                "tool_use_id" in str(e) or "tool_result" in str(e)
            ):
                logger.warning("Stale conversation history, resetting.")
                db.clear_history(chat_id)
                history = [{"role": "user", "content": trigger_message}]
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
            else:
                raise

        # Log token usage (Haiku: $0.80/M input, $4.00/M output)
        in_tok = response.usage.input_tokens
        out_tok = response.usage.output_tokens
        cost = (in_tok * 0.80 + out_tok * 4.00) / 1_000_000
        tool_names = [b.name for b in response.content if b.type == "tool_use"]
        step = ", ".join(tool_names) if tool_names else "reply"
        logger.info(f"[tokens] in={in_tok} out={out_tok} cost=${cost:.5f} | {step}")
        db.log_event(
            chat_id, "llm_call",
            tools_called=tool_names,
            in_tokens=in_tok, out_tokens=out_tok, cost_usd=round(cost, 6),
        )

        text_parts = [b.text for b in response.content if b.type == "text"]
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        # Send text to Telegram
        if text_parts:
            reply_text = "\n".join(text_parts)
            await bot.send_message(chat_id=chat_id, text=reply_text)
            db.log_event(chat_id, "assistant_reply", content=reply_text)

        # Persist assistant turn
        serialized = _serialize_content(response.content)
        history.append({"role": "assistant", "content": serialized})
        db.append_message(chat_id, "assistant", serialized)

        if not tool_uses:
            break

        # Execute tools
        tool_results = []
        for tu in tool_uses:
            # Send progress notification for slow tools
            if tu.name == "fetch_available_slots":
                ev_name = tu.input.get("event_name", "")
                ev_cfg = next(
                    (e for e in config["events"]
                     if e["name"].lower() == ev_name.lower()),
                    None,
                )
                if ev_cfg:
                    domain = ev_cfg["booking_url"].split("/")[2]
                    shop = ev_cfg.get("shop_name", domain)
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"_(Checking {ev_name} availability at {shop}...)_",
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="_(Checking availability...)_",
                        parse_mode="Markdown",
                    )

            db.log_event(chat_id, "tool_call", tool=tu.name, inputs=tu.input)
            try:
                result = await _execute_tool(tu.name, tu.input, config)
                db.log_event(
                    chat_id, "tool_result",
                    tool=tu.name, result_preview=result[:300],
                )
            except Exception as e:
                logger.exception(f"Tool {tu.name} failed")
                result = f"Error running {tu.name}: {e}"
                db.log_event(chat_id, "tool_error", tool=tu.name, error=str(e))

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result,
            })

        history.append({"role": "user", "content": tool_results})
        db.append_message(chat_id, "user", tool_results)

    # Trim old history
    db.trim_history(chat_id, keep=MAX_HISTORY)


# -- Telegram handlers ---------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    text = (update.message.text or "").strip()
    try:
        await run_agent(context.bot, chat_id, text)
    except Exception as e:
        logger.exception("run_agent failed")
        await update.message.reply_text(f"Error: {e}")


# -- Entry point ---------------------------------------------------------------

def run():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Initialize SQLite
    db.init_db()

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    scheduler = AsyncIOScheduler()

    async def post_init(application):
        async def scheduled_check():
            await run_agent(
                application.bot,
                chat_id,
                "Please check my calendar now and let me know if any recurring "
                "appointments are coming up that I haven't booked yet.",
            )

        scheduler.add_job(
            scheduled_check, "interval", hours=24, next_run_time=datetime.now()
        )
        scheduler.start()
        logger.info("Scheduler started.")

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Calendar Assistant starting. Listening for Telegram messages...")
    app.run_polling(allowed_updates=["message"])
