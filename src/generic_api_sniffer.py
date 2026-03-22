"""
Generic booking availability sniffer.

Intercepts all JSON API calls during page navigation, then uses Claude
to identify and parse slot data from the captured responses.

Works for any booking site that loads availability via internal JSON APIs
(the vast majority of modern SPA-based booking platforms).
"""

import asyncio
import json
import logging
import os

import anthropic
from playwright.async_api import async_playwright, Response

logger = logging.getLogger(__name__)

# Skip these — analytics/tracking noise
_SKIP = [
    "analytics", "telemetry", "tracking", "segment", "mixpanel",
    "hotjar", "sentry", "datadog", "amplitude", "heap", "gtm",
    "facebook", "twitter", "linkedin", "ads", "beacon",
]

# Prefer APIs with these patterns — likely availability/booking data
_PRIORITY = [
    "availab", "slot", "opening", "booking", "appointment", "schedule",
    "calendar", "staff", "service", "treatment", "time", "date",
]

# Click targets to trigger availability loading
_CLICK_TRIGGERS = [
    "Book Now", "Book Appointment", "Book", "Schedule", "Reserve",
    "Check Availability", "Availability", "Appointments", "See Times",
]


def _is_priority(url: str) -> bool:
    u = url.lower()
    return any(p in u for p in _PRIORITY)


def _should_skip(url: str) -> bool:
    u = url.lower()
    return any(p in u for p in _SKIP)


async def sniff_availability(booking_url: str, event_name: str, preferences: str) -> dict:
    """
    Navigate the booking site, capture JSON API responses, use Claude to find slots.
    Returns {"slots": [...], "message": "..."}
    """
    priority_apis: list[dict] = []
    all_apis: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        async def on_response(response: Response):
            url = response.url
            if response.status != 200 or _should_skip(url):
                return
            if "json" not in response.headers.get("content-type", ""):
                return
            try:
                data = await response.json()
                entry = {"url": url, "data": data}
                all_apis.append(entry)
                if _is_priority(url):
                    priority_apis.append(entry)
                    logger.info(f"[sniffer] Priority API: {url}")
            except Exception:
                pass

        page.on("response", on_response)

        logger.info(f"[sniffer] Loading {booking_url}")
        # Use domcontentloaded — many SPAs (Booksy, etc.) never reach networkidle
        await page.goto(booking_url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)  # let JS hydrate and fire initial API calls

        # Strategy 1: click a Book button near the event name (service-card pattern)
        # Works on Booksy-style pages where each service has its own Book button
        clicked = False
        for svc_kw in [event_name, event_name.split()[0]]:
            try:
                section = page.locator("*").filter(has_text=svc_kw).filter(
                    has=page.get_by_text("Book", exact=False)
                ).last
                book_btn = section.get_by_text("Book", exact=False).last
                if await book_btn.is_visible(timeout=1500):
                    await book_btn.click()
                    await page.wait_for_timeout(3000)
                    logger.info(f"[sniffer] Clicked service-specific Book near '{svc_kw}'")
                    clicked = True
                    break
            except Exception:
                continue

        # Strategy 2: fallback to generic page-level triggers
        if not clicked:
            for keyword in _CLICK_TRIGGERS:
                try:
                    el = page.get_by_text(keyword, exact=False).first
                    if await el.is_visible(timeout=1500):
                        await el.click()
                        await page.wait_for_timeout(2000)
                        logger.info(f"[sniffer] Clicked generic trigger: '{keyword}'")
                        break
                except Exception:
                    continue

        await browser.close()

    logger.info(
        f"[sniffer] Captured {len(all_apis)} APIs total, "
        f"{len(priority_apis)} priority for {event_name}"
    )

    if not all_apis:
        return {
            "slots": [],
            "message": (
                "Could not capture any API data from this booking site. "
                "It may require login or use a non-standard loading pattern."
            ),
        }

    # Prefer priority APIs; fall back to all (up to 8 entries)
    apis_to_analyze = (priority_apis or all_apis)[:8]

    def _truncate(data, max_chars: int = 2500) -> str:
        s = json.dumps(data, default=str)
        return s[:max_chars] + ("…" if len(s) > max_chars else "")

    api_text = "\n\n".join(
        f"URL: {e['url']}\n{_truncate(e['data'])}"
        for e in apis_to_analyze
    )

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": (
                    f"These JSON API responses were captured from a booking website "
                    f"while looking for {event_name} appointments.\n"
                    f"User preferences: {preferences}\n\n"
                    f"{api_text}\n\n"
                    "Extract any available appointment slots. "
                    'Return ONLY a JSON array of human-readable strings like '
                    '["Sat Mar 22 – 10:00 AM", "Sat Mar 22 – 2:00 PM"]. '
                    "Include date, time, and practitioner name if available. "
                    "Return [] if no slots found. No explanation, just the array."
                ),
            }
        ],
    )

    raw = response.content[0].text.strip()
    # Strip markdown fences if present
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        slots = json.loads(raw)
        if isinstance(slots, list) and all(isinstance(s, str) for s in slots):
            return {"slots": slots[:8], "message": ""}
    except Exception:
        pass

    return {"slots": [], "message": "Could not parse available slots from this site's API data."}


def get_available_slots_generic(booking_url: str, event_name: str, preferences: str) -> dict:
    """Sync wrapper."""
    return asyncio.run(sniff_availability(booking_url, event_name, preferences))
