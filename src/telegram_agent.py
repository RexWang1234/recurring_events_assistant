"""
Long-running Telegram bot + daily calendar scheduler.

Runs two async tasks concurrently:
  1. Telegram polling — handles user replies, drives the booking conversation
  2. APScheduler — fires a calendar check every 24 hours (and on startup)

Conversation state machine (per event):
  idle → awaiting_confirm → fetching_slots → awaiting_slot_choice
       → awaiting_final_confirm → booking_complete
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


# ── Calendar check + alert ─────────────────────────────────────────────────────

async def check_calendar_and_alert(bot: Bot, chat_id: str):
    """
    For each configured event: check Apple Calendar.
    If no upcoming booking and due date is within alert_days_before, send a Telegram alert.
    Skip if an alert was already sent today for that event.
    """
    config = _load_config()
    state = _load_state()
    today = datetime.now(timezone.utc).date().isoformat()

    for ev in config["events"]:
        event_key = ev["name"]
        ev_state = state.get(event_key, {})

        # Skip if already in an active booking conversation
        if ev_state.get("conversation_state") not in (None, "idle", "booking_complete"):
            logger.info(f"[{event_key}] Skipping check — active conversation.")
            continue

        # Skip if alert already sent today
        if ev_state.get("last_alert_date") == today:
            logger.info(f"[{event_key}] Alert already sent today.")
            continue

        status = get_event_status(ev)
        days_until = status["days_until_due"]
        next_scheduled = status["next_scheduled"]

        logger.info(
            f"[{event_key}] days_until_due={days_until}, next_scheduled={next_scheduled}"
        )

        # If there's already a future appointment booked, no action needed
        if next_scheduled is not None:
            logger.info(f"[{event_key}] Already booked for {next_scheduled.date()}. No alert.")
            continue

        # Alert if within the reminder window
        if days_until is None:
            # No past occurrences found — alert regardless
            msg = (
                f"📅 *{ev['name']}* — I couldn't find any past {ev['name'].lower()} in your calendar. "
                f"It looks like it's overdue! Want me to check availability?\n\nReply *yes* to proceed or *skip* to ignore."
            )
        elif days_until <= ev["alert_days_before"]:
            due_str = (datetime.now(timezone.utc) + timedelta(days=days_until)).strftime("%a %b %-d")
            msg = (
                f"📅 *{ev['name']}* is due around {due_str} ({days_until} day(s) away) "
                f"and you don't have one booked yet.\n\n"
                f"Want me to check availability at the {ev['name'].lower()} place?\n\n"
                f"Reply *yes* to proceed or *skip* to ignore."
            )
        else:
            logger.info(f"[{event_key}] Due in {days_until} days — outside alert window ({ev['alert_days_before']}d).")
            continue

        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

        state[event_key] = {
            **ev_state,
            "conversation_state": "awaiting_confirm",
            "last_alert_date": today,
            "booking_url": ev["booking_url"],
            "booking_preferences": ev["booking_preferences"],
            "event_config": ev,
        }
        _save_state(state)
        logger.info(f"[{event_key}] Alert sent.")


# ── Telegram message handler ───────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route incoming Telegram messages to the right conversation handler."""
    text = (update.message.text or "").strip().lower()
    chat_id = str(update.effective_chat.id)

    state = _load_state()

    # Find which event has an active conversation
    active_event = None
    for event_key, ev_state in state.items():
        conv_state = ev_state.get("conversation_state")
        if conv_state and conv_state not in ("idle", "booking_complete"):
            active_event = event_key
            break

    if active_event is None:
        await update.message.reply_text(
            "No active booking conversation. I'll message you when something is due! 👍"
        )
        return

    ev_state = state[active_event]
    conv_state = ev_state.get("conversation_state")
    config = _load_config()
    ev_config = next((e for e in config["events"] if e["name"] == active_event), ev_state.get("event_config", {}))

    # ── awaiting_confirm ────────────────────────────────────────────────────
    if conv_state == "awaiting_confirm":
        if text in ("yes", "y", "sure", "ok", "yeah", "yep"):
            await update.message.reply_text(
                f"Got it! Let me check availability for *{active_event}*... 🔍\n"
                f"_(This may take a minute while I browse the site)_",
                parse_mode="Markdown",
            )
            state[active_event]["conversation_state"] = "fetching_slots"
            _save_state(state)

            # Run in background so Telegram doesn't time out
            asyncio.create_task(
                _fetch_slots_and_reply(
                    bot=context.bot,
                    chat_id=chat_id,
                    active_event=active_event,
                    ev_config=ev_config,
                    ev_state=ev_state,
                )
            )

        elif text in ("no", "n", "skip", "later", "nope"):
            state[active_event]["conversation_state"] = "idle"
            _save_state(state)
            await update.message.reply_text(
                f"No problem! I'll remind you again next time. 👍"
            )
        else:
            await update.message.reply_text("Please reply *yes* to check availability or *skip* to ignore.", parse_mode="Markdown")

    # ── awaiting_slot_choice ────────────────────────────────────────────────
    elif conv_state == "awaiting_slot_choice":
        slots = ev_state.get("slots", [])
        if text == "skip":
            state[active_event]["conversation_state"] = "idle"
            _save_state(state)
            await update.message.reply_text("OK, skipping for now. I'll remind you again later. 👍")
            return

        # Accept a number (1, 2, 3...) or "refresh"
        if text == "refresh":
            state[active_event]["conversation_state"] = "fetching_slots"
            _save_state(state)
            await update.message.reply_text("Refreshing available slots... 🔄")
            asyncio.create_task(
                _fetch_slots_and_reply(
                    bot=context.bot,
                    chat_id=chat_id,
                    active_event=active_event,
                    ev_config=ev_config,
                    ev_state=ev_state,
                )
            )
            return

        try:
            choice = int(text)
            if 1 <= choice <= len(slots):
                chosen_slot = slots[choice - 1]
                state[active_event]["chosen_slot"] = chosen_slot
                state[active_event]["conversation_state"] = "awaiting_final_confirm"
                _save_state(state)
                await update.message.reply_text(
                    f"You chose: *{chosen_slot}*\n\n"
                    f"I'll now fill in the booking form and show you a summary before confirming.\n"
                    f"_(Browsing the site...)_",
                    parse_mode="Markdown",
                )
                asyncio.create_task(
                    _prepare_booking_and_confirm(
                        bot=context.bot,
                        chat_id=chat_id,
                        active_event=active_event,
                        ev_config=ev_config,
                        ev_state=ev_state,
                        chosen_slot=chosen_slot,
                    )
                )
            else:
                await update.message.reply_text(
                    f"Please reply with a number between 1 and {len(slots)}, or *skip*.",
                    parse_mode="Markdown",
                )
        except ValueError:
            await update.message.reply_text(
                f"Please reply with a number (1–{len(slots)}) to choose a slot, or *skip*.",
                parse_mode="Markdown",
            )

    # ── awaiting_final_confirm ──────────────────────────────────────────────
    elif conv_state == "awaiting_final_confirm":
        if text in ("confirm", "yes", "y", "book it", "proceed"):
            chosen_slot = ev_state.get("chosen_slot", "")
            user_info = _load_user_info()

            await update.message.reply_text("Submitting your booking now... ⏳")
            state[active_event]["conversation_state"] = "booking"
            _save_state(state)

            asyncio.create_task(
                _submit_booking(
                    bot=context.bot,
                    chat_id=chat_id,
                    active_event=active_event,
                    ev_config=ev_config,
                    chosen_slot=chosen_slot,
                    user_info=user_info,
                )
            )

        elif text in ("cancel", "no", "n", "back"):
            state[active_event]["conversation_state"] = "awaiting_slot_choice"
            _save_state(state)
            slots = ev_state.get("slots", [])
            slots_text = _format_slots(slots)
            await update.message.reply_text(
                f"OK, cancelled. Here are the slots again:\n\n{slots_text}\n\nReply with a number or *skip*.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "Reply *confirm* to complete the booking, or *cancel* to go back.",
                parse_mode="Markdown",
            )

    else:
        await update.message.reply_text(
            "I'm not sure what to do with that right now. I'll reach out when your next appointment is due! 👍"
        )


# ── Async background tasks ─────────────────────────────────────────────────────

def _format_slots(slots: list[str]) -> str:
    return "\n".join(f"  *{i+1}.* {slot}" for i, slot in enumerate(slots))


async def _fetch_slots_and_reply(bot, chat_id, active_event, ev_config, ev_state):
    """Background task: browse booking site, collect slots, message user."""
    state = _load_state()
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: get_available_slots(
                booking_url=ev_config["booking_url"],
                event_name=active_event,
                preferences=ev_config.get("booking_preferences", ""),
            ),
        )
        slots = result.get("slots", [])
        note = result.get("message", "")

        if not slots:
            msg = f"😔 No available slots found for *{active_event}*."
            if note:
                msg += f"\n\n_{note}_"
            msg += "\n\nReply *retry* to try again or *skip* to ignore."
            state[active_event]["conversation_state"] = "awaiting_confirm"
        else:
            slots_text = _format_slots(slots)
            msg = f"Here are the available slots for *{active_event}*:\n\n{slots_text}"
            if note:
                msg += f"\n\n_{note}_"
            msg += "\n\nReply with a number to choose, *refresh* to reload, or *skip* to ignore."
            state[active_event]["slots"] = slots
            state[active_event]["conversation_state"] = "awaiting_slot_choice"

    except Exception as e:
        logger.exception(f"[{active_event}] Error fetching slots")
        msg = f"⚠️ Something went wrong while checking availability: {e}\n\nReply *yes* to try again."
        state[active_event]["conversation_state"] = "awaiting_confirm"

    _save_state(state)
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")


async def _prepare_booking_and_confirm(bot, chat_id, active_event, ev_config, ev_state, chosen_slot):
    """Background task: fill the booking form, show summary, wait for final confirm."""
    state = _load_state()
    user_info = _load_user_info()
    try:
        summary = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: complete_booking(
                booking_url=ev_config["booking_url"],
                event_name=active_event,
                preferences=ev_config.get("booking_preferences", ""),
                chosen_slot=chosen_slot,
                user_info=user_info,
            ),
        )
        msg = (
            f"📋 *Booking summary for {active_event}*\n\n"
            f"```\n{summary[:800]}\n```\n\n"
            f"Reply *confirm* to complete this booking, or *cancel* to go back."
        )
        state[active_event]["conversation_state"] = "awaiting_final_confirm"
    except Exception as e:
        logger.exception(f"[{active_event}] Error preparing booking")
        msg = f"⚠️ Error preparing the booking: {e}\n\nReply *yes* to start over."
        state[active_event]["conversation_state"] = "awaiting_confirm"

    _save_state(state)
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")


async def _submit_booking(bot, chat_id, active_event, ev_config, chosen_slot, user_info):
    """Background task: click the final confirm button."""
    state = _load_state()
    try:
        confirmation = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: final_submit(
                booking_url=ev_config["booking_url"],
                event_name=active_event,
                chosen_slot=chosen_slot,
                user_info=user_info,
            ),
        )
        msg = (
            f"✅ *{active_event} booked!*\n\n"
            f"```\n{confirmation[:600]}\n```\n\n"
            f"I'll keep an eye on your calendar for the next one. 📅"
        )
        state[active_event]["conversation_state"] = "booking_complete"
    except Exception as e:
        logger.exception(f"[{active_event}] Error submitting booking")
        msg = f"⚠️ The booking submission failed: {e}\n\nPlease book manually or reply *yes* to retry."
        state[active_event]["conversation_state"] = "awaiting_confirm"

    _save_state(state)
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")


# ── Agent entry point ──────────────────────────────────────────────────────────

def run():
    """Start the Telegram bot and daily scheduler. Blocks until stopped."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler()

    async def scheduled_check():
        await check_calendar_and_alert(app.bot, chat_id)

    # Run immediately on startup, then every 24 hours
    scheduler.add_job(scheduled_check, "interval", hours=24, next_run_time=datetime.now())
    scheduler.start()

    logger.info("Calendar Assistant starting. Listening for Telegram messages...")
    app.run_polling(allowed_updates=["message"])
