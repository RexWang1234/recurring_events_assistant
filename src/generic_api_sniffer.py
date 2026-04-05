"""
Generic booking availability sniffer.

Intercepts all JSON API calls during page navigation, then uses Claude
to identify and parse slot data from the captured responses.

This is the FALLBACK for sites without a dedicated scraper. Claude is only
used here to interpret unknown API schemas -- all filtering and ranking
happens deterministically in slot_service.py.
"""

import asyncio
import json
import logging
import os
from datetime import datetime

import anthropic
from playwright.async_api import async_playwright, Response

from src.models import Slot

logger = logging.getLogger(__name__)

# Skip analytics/tracking noise
_SKIP = [
    "analytics", "telemetry", "tracking", "segment", "mixpanel",
    "hotjar", "sentry", "datadog", "amplitude", "heap", "gtm",
    "facebook", "twitter", "linkedin", "ads", "beacon",
]

# Prefer APIs with these URL patterns -- likely availability/booking data
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


async def _sniff_apis(booking_url: str, event_name: str) -> list[dict]:
    """Navigate site, capture all JSON API responses."""
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
        await page.goto(booking_url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)

        # Try clicking a Book button near the service name
        clicked = False
        for svc_kw in [event_name, event_name.split()[0]]:
            try:
                section = (
                    page.locator("*")
                    .filter(has_text=svc_kw)
                    .filter(has=page.get_by_text("Book", exact=False))
                    .last
                )
                book_btn = section.get_by_text("Book", exact=False).last
                if await book_btn.is_visible(timeout=1500):
                    await book_btn.click()
                    await page.wait_for_timeout(3000)
                    logger.info(f"[sniffer] Clicked Book near '{svc_kw}'")
                    clicked = True
                    break
            except Exception:
                continue

        # Fallback: generic page-level triggers
        if not clicked:
            for keyword in _CLICK_TRIGGERS:
                try:
                    el = page.get_by_text(keyword, exact=False).first
                    if await el.is_visible(timeout=1500):
                        await el.click()
                        await page.wait_for_timeout(2000)
                        logger.info(f"[sniffer] Clicked trigger: '{keyword}'")
                        break
                except Exception:
                    continue

        await browser.close()

    logger.info(
        f"[sniffer] Captured {len(all_apis)} APIs total, "
        f"{len(priority_apis)} priority for {event_name}"
    )
    return (priority_apis or all_apis)[:8]


def _ask_claude_for_slots(apis: list[dict], event_name: str, preferences: str) -> list[dict]:
    """Use Claude to extract structured slot data from captured API responses.

    Returns list of dicts with keys: start, provider, service, duration_min.
    """
    if not apis:
        return []

    def _truncate(data, max_chars: int = 2500) -> str:
        s = json.dumps(data, default=str)
        return s[:max_chars] + ("\u2026" if len(s) > max_chars else "")

    api_text = "\n\n".join(
        f"URL: {e['url']}\n{_truncate(e['data'])}" for e in apis
    )

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"These JSON API responses were captured from a booking website "
                f"while looking for {event_name} appointments.\n\n"
                f"{api_text}\n\n"
                "Extract available appointment slots as a JSON array of objects. "
                "Each object should have: "
                '"start" (ISO datetime string), '
                '"provider" (practitioner name or null), '
                '"service" (service name or null), '
                '"duration_min" (integer or null). '
                "Return ONLY the JSON array, no explanation. "
                "Return [] if no slots found."
            ),
        }],
    )

    raw = response.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return result
    except Exception:
        pass
    return []


def scrape_generic_slots(
    booking_url: str,
    event_name: str = "",
    preferences: str = "",
    shop_name: str = "",
) -> list[Slot]:
    """Main entry point -- returns normalized Slot objects.

    Uses Playwright to capture JSON APIs, then Claude to parse them
    into structured slot data.
    """
    try:
        apis = asyncio.run(_sniff_apis(booking_url, event_name))
        if not apis:
            logger.warning("[sniffer] No APIs captured")
            return []

        raw_slots = _ask_claude_for_slots(apis, event_name, preferences)
        if not raw_slots:
            return []

        slots: list[Slot] = []
        for raw in raw_slots:
            start_str = raw.get("start")
            if not start_str:
                continue
            try:
                dt = datetime.fromisoformat(str(start_str))
                dt_local = dt.astimezone()
            except Exception:
                continue

            slots.append(Slot(
                shop=shop_name,
                service=raw.get("service") or event_name,
                provider=raw.get("provider"),
                start_time=dt_local,
                end_time=None,
                duration_min=raw.get("duration_min"),
                source_url=booking_url,
                platform="generic",
            ))

        slots.sort(key=lambda s: s.start_time)
        return slots

    except Exception as e:
        logger.exception("[sniffer] Generic scraper failed")
        return []
