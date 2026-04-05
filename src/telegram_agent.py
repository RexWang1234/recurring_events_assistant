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
from src.booking_agent import get_available_slots_multi
from src.slot_service import get_shops
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
            "Check booking websites to find available appointment slots. "
            "If the event has multiple shops, checks all of them (or just one if shop_name is specified). "
            "Returns a pre-filtered, ranked list of slots. Takes ~30-60 seconds per shop."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_name": {
                    "type": "string",
                    "description": "Name of the event (e.g. 'Massage', 'Haircut')",
                },
                "shop_name": {
                    "type": "string",
                    "description": "Optional: specific shop to check. If omitted, checks all shops for this event.",
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
            last = status["last_occurrence"]
            next_due = status["next_due"]
            days = status["days_until_due"]
            booked = status["next_scheduled"]

            parts = [f"Event: {ev['name']}"]
            shops = get_shops(ev)
            shop_names = ", ".join(s["name"] for s in shops) if shops else "N/A"
            parts.append(f"  Shop(s): {shop_names}")
            if last:
                parts.append(f"  Last visit: {last.strftime('%b %-d, %Y')}")
                if days < 0:
                    parts.append(f"  Next due: {next_due.strftime('%b %-d, %Y')} ({abs(days)} days overdue)")
                else:
                    parts.append(f"  Next due: {next_due.strftime('%b %-d, %Y')} ({days} days from now)")
            else:
                parts.append("  Last visit: No record found")
            if booked:
                parts.append(f"  Status: BOOKED on {booked.strftime('%b %-d, %Y')}")
            elif days is not None and days < 0:
                parts.append(f"  Status: OVERDUE by {abs(days)} days -- not booked")
            elif days is not None and days <= ev["alert_days_before"]:
                parts.append("  Status: DUE SOON -- not booked")
            else:
                parts.append("  Status: Not yet due")
            lines.append("\n".join(parts))
        return "\n\n".join(lines) if lines else "No events configured."

    elif name == "fetch_available_slots":
        event_name = inputs["event_name"]
        shop_filter = inputs.get("shop_name", "")
        ev_config = next(
            (e for e in config["events"]
             if e["name"].lower() == event_name.lower()),
            None,
        )
        if ev_config is None:
            return f"Unknown event: {event_name}"

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: get_available_slots_multi(ev_config, shop_filter),
        )
        return result["display"]

    return f"Unknown tool: {name}"


def _build_system_prompt(config: dict, user_info: dict) -> str:
    today = datetime.now().strftime("%A, %B %-d, %Y")
    events_lines = []
    for e in config.get("events", []):
        shops = get_shops(e)
        shop_desc = ", ".join(f"{s['name']} ({s['url']})" for s in shops)
        events_lines.append(
            f"- {e['name']}: every {e['frequency_weeks']} week(s), "
            f"alert {e['alert_days_before']} days before due, "
            f"shop(s): {shop_desc}"
        )
    events_desc = "\n".join(events_lines)
    user_desc = ""
    if user_info:
        user_desc = "\nUser info:\n" + "\n".join(
            f"  {k}: {v}" for k, v in user_info.items()
        )
    return f"""You are a friendly personal calendar assistant running on the user's MacBook.

Today is {today}.

Configured recurring events:
{events_desc}{user_desc}

FORMATTING: You are sending messages via Telegram. Do NOT use markdown tables, \
bold (**), italic, or any markdown syntax. Use plain text with emojis. Use line \
breaks and emojis to structure your messages and make them feel friendly.

Your job:
- When checking the calendar (scheduled or on request): call get_calendar_status first.
- After receiving calendar status, present a clear summary to the user. For example:

  💆 Massage - OVERDUE by 14 days (last: Mar 9). Not booked yet.
  ✂️ Haircut - Due in 25 days (May 1). On track.
  🧘 Yoga - No record. Not yet due.

  Your massage is overdue! Want me to check availability? 😊

- Do NOT automatically call fetch_available_slots for every event. Only fetch \
slots when the user asks for a specific event or confirms they want you to check.
- When presenting slots, use a numbered list and include the booking link.
- Be warm, concise, and conversational -- not robotic or menu-driven.
- Booking submission is not available through the bot -- direct the user to the booking link.
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


def _strip_tool_messages(history: list[dict]) -> list[dict]:
    """Remove tool_use/tool_result messages but keep text conversation.

    This preserves conversational context when stale tool_use_ids cause
    400 errors. Assistant messages with tool_use blocks are replaced with
    just their text content. User messages containing tool_results are dropped.
    """
    cleaned = []
    for msg in history:
        role = msg["role"]
        content = msg["content"]

        if role == "user":
            # Drop tool_result messages (list of {"type": "tool_result", ...})
            if isinstance(content, list) and content and isinstance(content[0], dict):
                if content[0].get("type") == "tool_result":
                    continue
            cleaned.append(msg)

        elif role == "assistant":
            # Keep only text blocks, drop tool_use blocks
            if isinstance(content, list):
                text_blocks = [
                    b for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
                ]
                if text_blocks:
                    cleaned.append({"role": "assistant", "content": text_blocks})
                # If no text blocks (pure tool call), skip the message
            else:
                cleaned.append(msg)

    # Ensure history alternates user/assistant and starts with user
    if cleaned and cleaned[0]["role"] != "user":
        cleaned = cleaned[1:]

    return cleaned


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
                # Strip tool_use/tool_result messages but keep text conversation
                logger.warning("Stale tool IDs in history, stripping tool messages.")
                history = _strip_tool_messages(history)
                history.append({"role": "user", "content": trigger_message})
                # Rebuild DB from cleaned history
                db.clear_history(chat_id)
                for msg in history:
                    db.append_message(chat_id, msg["role"], msg["content"])
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
                shop_filter = tu.input.get("shop_name", "")
                ev_cfg = next(
                    (e for e in config["events"]
                     if e["name"].lower() == ev_name.lower()),
                    None,
                )
                if ev_cfg:
                    shops = get_shops(ev_cfg)
                    if shop_filter:
                        label = shop_filter
                    elif len(shops) == 1:
                        label = shops[0]["name"]
                    else:
                        label = f"{len(shops)} shops"
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"Checking {ev_name} availability at {label}...",
                    )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="Checking availability...",
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
                "Check my calendar status and show me a summary. "
                "If anything is overdue or due soon, suggest I check availability.",
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
