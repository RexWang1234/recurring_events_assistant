"""
AI-powered booking agent.
Uses Claude + Playwright to navigate a booking website.

Two modes:
  get_available_slots()  — browse the site and return a list of available slots
  complete_booking()     — given a chosen slot, fill the form and confirm
  book_appointment()     — fully autonomous end-to-end (used by CLI)
"""

import asyncio
import os
from typing import Any

import anthropic
from playwright.async_api import async_playwright

# ── Browser tools available to Claude ─────────────────────────────────────────

BROWSER_TOOLS = [
    {
        "name": "navigate",
        "description": "Navigate the browser to a URL.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The URL to navigate to"}},
            "required": ["url"],
        },
    },
    {
        "name": "click",
        "description": "Click an element on the page by CSS selector or visible text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector OR visible text to click (e.g. 'Book Now' or '#submit-btn')",
                }
            },
            "required": ["selector"],
        },
    },
    {
        "name": "press_key",
        "description": "Press a keyboard key. Useful for dismissing modals (Escape), confirming (Enter), or navigating.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key name e.g. 'Escape', 'Enter', 'Tab', 'ArrowRight'"}
            },
            "required": ["key"],
        },
    },
    {
        "name": "wait",
        "description": "Wait for a short period to let the page load or animate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ms": {"type": "integer", "description": "Milliseconds to wait (max 3000)"}
            },
            "required": ["ms"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into a focused or selected input field.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of the input field"},
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["selector", "text"],
        },
    },
    {
        "name": "get_page_text",
        "description": "Get the visible text content of the current page. Use this as your primary way to read slot availability.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "select_option",
        "description": "Select an option from a <select> dropdown.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of the <select> element"},
                "value": {"type": "string", "description": "The option value or label to select"},
            },
            "required": ["selector", "value"],
        },
    },
]

# Extra tool for the "find slots" phase — Claude calls this when done scanning
RETURN_SLOTS_TOOL = {
    "name": "return_slots",
    "description": (
        "Call this when you have found the available appointment time slots. "
        "Pass them as a list of human-readable strings."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slots": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of available slots, e.g. ['Tue Mar 25 – 2:00pm', 'Wed Mar 26 – 10:00am']",
            },
            "message": {
                "type": "string",
                "description": "Optional note to the user (e.g. 'No availability this week, showing next week')",
            },
        },
        "required": ["slots"],
    },
}


async def _execute_tool(page, tool_name: str, tool_input: dict) -> Any:
    if tool_name == "navigate":
        await page.goto(tool_input["url"], wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        return f"Navigated to {tool_input['url']}"

    elif tool_name == "click":
        selector = tool_input["selector"]
        try:
            # Try CSS selector first
            await page.click(selector, timeout=3000)
        except Exception:
            # Fall back to text match, but force-click to bypass overlays
            await page.get_by_text(selector, exact=False).first.click(force=True, timeout=5000)
        await page.wait_for_timeout(1000)
        return f"Clicked '{selector}'"

    elif tool_name == "press_key":
        key = tool_input["key"]
        await page.keyboard.press(key)
        await page.wait_for_timeout(500)
        return f"Pressed '{key}'"

    elif tool_name == "wait":
        ms = min(tool_input.get("ms", 1000), 3000)
        await page.wait_for_timeout(ms)
        return f"Waited {ms}ms"

    elif tool_name == "type_text":
        await page.fill(tool_input["selector"], tool_input["text"])
        await page.wait_for_timeout(500)
        return f"Typed into {tool_input['selector']}"

    elif tool_name == "get_page_text":
        return await page.inner_text("body")

    elif tool_name == "select_option":
        await page.select_option(tool_input["selector"], label=tool_input["value"])
        await page.wait_for_timeout(500)
        return f"Selected '{tool_input['value']}'"

    return f"Unknown tool: {tool_name}"


# ── Phase 1: Get available slots ──────────────────────────────────────────────

async def _run_get_slots(booking_url: str, event_name: str, preferences: str) -> dict:
    """
    Browse the booking site and return available slots.
    Returns {"slots": [...], "message": "..."}
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    system_prompt = f"""You are a booking assistant helping the user find available {event_name} appointment slots.

User preferences: {preferences}

Your task:
1. Navigate to the booking page.
2. If a modal or popup appears (e.g. "next available", cookie notice, welcome dialog), dismiss it immediately by pressing Escape or clicking outside it.
3. Use get_page_text to read the page content. Use click, press_key, and wait to navigate.
4. Browse available times. On Jane App sites: select a service/practitioner, then read the calendar for open slots. Navigate forward weeks if needed.
5. Collect up to 6 available slots matching the user's preferences.
6. Call return_slots with human-readable strings like "Tue Mar 25 – 2:00pm with Jane Smith".

Do NOT log in. Do NOT complete the booking. Just find and return available slots.
If no slots are found, call return_slots with an empty list and explain why.
"""

    messages = [
        {
            "role": "user",
            "content": f"Please find available {event_name} appointment slots at {booking_url}. Preferences: {preferences}",
        }
    ]

    tools = BROWSER_TOOLS + [RETURN_SLOTS_TOOL]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        try:
            for _ in range(20):
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=2048,
                    system=system_prompt,
                    tools=tools,
                    messages=messages,
                )

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "end_turn":
                    break

                tool_results = []
                for tool_use in tool_uses:
                    if tool_use.name == "return_slots":
                        await browser.close()
                        return {
                            "slots": tool_use.input.get("slots", []),
                            "message": tool_use.input.get("message", ""),
                        }

                    result = await _execute_tool(page, tool_use.name, tool_use.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": str(result)[:4000],
                    })

                messages.append({"role": "user", "content": tool_results})

        except Exception as e:
            await browser.close()
            raise e

        await browser.close()
        return {"slots": [], "message": "Could not find available slots (max iterations reached)."}


# ── Phase 2: Complete booking for a chosen slot ───────────────────────────────

async def _run_complete_booking(
    booking_url: str,
    event_name: str,
    preferences: str,
    chosen_slot: str,
    user_info: dict,
) -> str:
    """Complete the booking for a specific chosen slot. Returns a confirmation summary."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    system_prompt = f"""You are a booking assistant completing a {event_name} appointment booking.

The user has chosen this slot: {chosen_slot}
User preferences: {preferences}
User info for forms: {user_info}

Your task:
1. Navigate to the booking page.
2. Find and select the slot: {chosen_slot}
3. Fill in required personal details using the user info provided.
4. STOP before the final confirmation/submit button and describe what you are about to confirm.
   Do NOT click the final submit unless the user has explicitly pre-approved.
   Instead, use get_page_text to capture the confirmation summary and return it.

If the slot is no longer available, report that clearly.
Do NOT make up information. Skip optional fields if data is not provided.
"""

    messages = [
        {
            "role": "user",
            "content": f"Please book the {event_name} appointment at {booking_url} for slot: {chosen_slot}",
        }
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        try:
            for _ in range(30):
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=2048,
                    system=system_prompt,
                    tools=BROWSER_TOOLS,
                    messages=messages,
                )

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                text_blocks = [b.text for b in response.content if b.type == "text"]
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "end_turn":
                    await browser.close()
                    return "\n".join(text_blocks)

                tool_results = []
                for tool_use in tool_uses:
                    result = await _execute_tool(page, tool_use.name, tool_use.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": str(result)[:4000],
                    })

                messages.append({"role": "user", "content": tool_results})

        except Exception as e:
            await browser.close()
            raise e

        await browser.close()
        return "Booking agent completed (max iterations reached)."


# ── Phase 2b: Final submit ────────────────────────────────────────────────────

async def _run_final_submit(booking_url: str, event_name: str, chosen_slot: str, user_info: dict) -> str:
    """Navigate back to confirm page and click the final submit button."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    system_prompt = f"""You are completing a {event_name} booking.
The user has confirmed they want to proceed.
Navigate to {booking_url}, find the slot {chosen_slot}, and complete the full booking including clicking the final confirmation button.
Return the booking confirmation details (date, time, confirmation number if shown).
User info: {user_info}
"""

    messages = [{"role": "user", "content": f"Complete the booking for {chosen_slot} at {booking_url}."}]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        try:
            for _ in range(30):
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=2048,
                    system=system_prompt,
                    tools=BROWSER_TOOLS,
                    messages=messages,
                )

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                text_blocks = [b.text for b in response.content if b.type == "text"]
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "end_turn":
                    await browser.close()
                    return "\n".join(text_blocks)

                tool_results = []
                for tool_use in tool_uses:
                    result = await _execute_tool(page, tool_use.name, tool_use.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": str(result)[:4000],
                    })

                messages.append({"role": "user", "content": tool_results})

        except Exception as e:
            await browser.close()
            raise e

        await browser.close()
        return "Booking submitted (max iterations reached)."


# ── Public sync wrappers ──────────────────────────────────────────────────────

def get_available_slots(booking_url: str, event_name: str, preferences: str) -> dict:
    """Return {"slots": [...], "message": "..."} without completing the booking.

    Routing strategy (fastest/most-reliable first):
      1. Jane App  → dedicated API-interception scraper (zero AI cost)
      2. Everything else → generic API sniffer (intercepts JSON APIs, Claude parses)

    The old AI browser agent (_run_get_slots) is retired — it required screenshots
    to work reliably and failed without them.
    """
    from src.jane_app_scraper import get_available_slots_jane
    from src.booksy_scraper import get_available_slots_booksy
    from src.generic_api_sniffer import get_available_slots_generic

    if "janeapp.com" in booking_url:
        # Dedicated scraper: intercepts known API endpoint, fastest, no AI cost
        return get_available_slots_jane(booking_url, event_name, preferences)

    if "booksy.com" in booking_url:
        # Dedicated scraper: follows Booksy's multi-step booking flow
        result = get_available_slots_booksy(booking_url, event_name, preferences)
        if result["slots"]:
            return result
        logger.warning("[booksy] Dedicated scraper got no slots, falling back to generic sniffer")
        return get_available_slots_generic(booking_url, event_name, preferences)

    # All other sites: generic API sniffer (captures JSON APIs, Claude parses result)
    return get_available_slots_generic(booking_url, event_name, preferences)


def complete_booking(
    booking_url: str,
    event_name: str,
    preferences: str,
    chosen_slot: str,
    user_info: dict,
) -> str:
    """Navigate to booking site, select the slot, fill form — stop before final submit."""
    return asyncio.run(_run_complete_booking(booking_url, event_name, preferences, chosen_slot, user_info))


def final_submit(booking_url: str, event_name: str, chosen_slot: str, user_info: dict) -> str:
    """Click the final confirm button and return booking confirmation."""
    return asyncio.run(_run_final_submit(booking_url, event_name, chosen_slot, user_info))


def book_appointment(booking_url: str, event_name: str, preferences: str, user_info: dict) -> str:
    """Fully autonomous end-to-end booking (used by CLI `book` command)."""
    return asyncio.run(_run_booking_agent(booking_url, event_name, preferences, user_info))


async def _run_booking_agent(
    booking_url: str,
    event_name: str,
    preferences: str,
    user_info: dict,
) -> str:
    """Fully autonomous booking loop (no user interaction)."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    system_prompt = f"""You are a booking assistant. Book a {event_name} appointment for the user.

User preferences: {preferences}
User info for forms: {user_info}

1. Navigate to the booking page.
2. Find available slots matching preferences.
3. Select the best slot.
4. Fill in required details.
5. Complete the booking and confirm.
6. Return a summary: date, time, confirmation number if any.

If you cannot complete the booking, explain why.
"""

    messages = [
        {"role": "user", "content": f"Book a {event_name} at {booking_url}. Preferences: {preferences}"}
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        try:
            for _ in range(30):
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=4096,
                    system=system_prompt,
                    tools=BROWSER_TOOLS,
                    messages=messages,
                )

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                text_blocks = [b.text for b in response.content if b.type == "text"]
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "end_turn":
                    await browser.close()
                    return "\n".join(text_blocks)

                tool_results = []
                for tool_use in tool_uses:
                    result = await _execute_tool(page, tool_use.name, tool_use.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": str(result)[:4000],
                    })

                messages.append({"role": "user", "content": tool_results})

        except Exception as e:
            await browser.close()
            raise e

        await browser.close()
        return "Booking agent completed (max iterations reached)."
