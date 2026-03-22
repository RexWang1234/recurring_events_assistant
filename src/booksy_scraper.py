"""
Booksy availability scraper.

Intercepts the Booksy internal API:
  GET /core/v2/customer_api/me/businesses/{id}/appointments/time_slots

Schema:
  {"time_slots": [{"date": "YYYY-MM-DD", "slots": [{"t": "HH:MM", "p": ""}, ...]}]}

The time_slots API fires automatically when the page loads with a service pre-selected
(e.g. via the hash fragment #ba_s=seo which Booksy uses for their embedded widgets).
"""

import asyncio
import logging
import re
from datetime import datetime

from playwright.async_api import async_playwright, Response

logger = logging.getLogger(__name__)

# How many additional weeks to request (Booksy loads 1 week at a time by default)
EXTRA_WEEKS = 2


async def _intercept_time_slots(booking_url: str) -> list[dict]:
    """Navigate Booksy, click Book next to the service, capture all time_slots responses."""
    captured: list[dict] = []

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
            if "time_slots" in response.url and response.status == 200:
                try:
                    data = await response.json()
                    if "time_slots" in data:
                        captured.extend(data["time_slots"])
                        logger.info(
                            f"[booksy] Captured {len(data['time_slots'])} date(s) from {response.url}"
                        )
                except Exception:
                    pass

        page.on("response", on_response)

        logger.info(f"[booksy] Loading {booking_url}")
        await page.goto(booking_url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)

        # Click "Book" next to the first (most popular) service to trigger the slot API
        # Booksy loads time_slots right after service selection
        for service_hint in ["Men's Hair Cut", "Haircut", "Hair Cut", "Book"]:
            try:
                section = page.locator("*").filter(has_text=service_hint).filter(
                    has=page.get_by_text("Book", exact=False)
                ).last
                book_btn = section.get_by_text("Book", exact=True).last
                if await book_btn.is_visible(timeout=2000):
                    await book_btn.click()
                    await page.wait_for_timeout(3000)
                    logger.info(f"[booksy] Clicked Book near '{service_hint}'")
                    break
            except Exception:
                continue

        # Navigate forward to load more weeks
        for _ in range(EXTRA_WEEKS):
            if len(captured) >= 3:
                break
            for nav_txt in ["Next week", "Next", "›", ">"]:
                try:
                    btn = page.get_by_text(nav_txt, exact=False).first
                    if await btn.is_visible(timeout=1500):
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        break
                except Exception:
                    continue

        await browser.close()

    return captured


def _parse_time_slots(date_entries: list[dict], preferences: str) -> list[str]:
    """
    Convert Booksy time_slot date entries to human-readable slot strings.

    Each entry: {"date": "YYYY-MM-DD", "slots": [{"t": "HH:MM", "p": ""}, ...]}
    Prefers times matching preferences (e.g. "morning", "afternoon", "Saturday").
    """
    pref_lower = preferences.lower()

    # Day-of-week preference
    preferred_days: set[int] = set()
    day_map = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    for day_name, day_num in day_map.items():
        if day_name in pref_lower:
            preferred_days.add(day_num)

    # Time-of-day preference
    prefer_morning = "morning" in pref_lower
    prefer_afternoon = "afternoon" in pref_lower or "pm" in pref_lower
    prefer_evening = "evening" in pref_lower

    result: list[tuple[datetime, str]] = []
    seen: set[str] = set()

    for entry in date_entries:
        date_str = entry.get("date", "")
        slots = entry.get("slots", [])
        if not date_str or not slots:
            continue

        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        except Exception:
            continue

        # Skip non-preferred days if preference is set
        if preferred_days and date_obj.weekday() not in preferred_days:
            continue

        for slot in slots:
            t = slot.get("t", "")
            if not t:
                continue
            try:
                hour, minute = map(int, t.split(":"))
                dt = date_obj.replace(hour=hour, minute=minute)
                dt_local = dt.astimezone()

                # Apply time-of-day filter if preference set
                if prefer_morning and hour >= 12:
                    continue
                if prefer_afternoon and (hour < 12 or hour >= 17):
                    continue
                if prefer_evening and hour < 17:
                    continue

                label = dt_local.strftime("%a %b %-d – %-I:%M %p")
                if label not in seen:
                    seen.add(label)
                    result.append((dt, label))
            except Exception:
                continue

    result.sort(key=lambda x: x[0])
    return [label for _, label in result[:8]]


async def _run(booking_url: str, event_name: str, preferences: str) -> dict:
    try:
        date_entries = await _intercept_time_slots(booking_url)
        logger.info(f"[booksy] Total date entries captured: {len(date_entries)}")

        if not date_entries:
            return {
                "slots": [],
                "message": "Could not load time slots from Booksy.",
            }

        slots = _parse_time_slots(date_entries, preferences)
        if not slots:
            return {
                "slots": [],
                "message": "No slots matched your preferences on Booksy.",
            }

        return {"slots": slots, "message": ""}

    except Exception as e:
        logger.exception("[booksy] Scraper failed")
        return {"slots": [], "message": f"Error checking Booksy availability: {e}"}


def get_available_slots_booksy(booking_url: str, event_name: str, preferences: str) -> dict:
    """Sync wrapper."""
    return asyncio.run(_run(booking_url, event_name, preferences))
