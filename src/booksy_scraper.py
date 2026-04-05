"""
Booksy availability scraper.

Intercepts the Booksy internal API:
  GET /core/v2/customer_api/me/businesses/{id}/appointments/time_slots

Schema:
  {"time_slots": [{"date": "YYYY-MM-DD", "slots": [{"t": "HH:MM", "p": ""}, ...]}]}
"""

import asyncio
import logging
from datetime import datetime

from playwright.async_api import async_playwright, Response

from src.models import Slot

logger = logging.getLogger(__name__)

EXTRA_WEEKS = 2


async def _intercept_time_slots(booking_url: str, event_name: str) -> list[dict]:
    """Navigate Booksy, click Book next to the service, capture time_slots responses."""
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
                            f"[booksy] Captured {len(data['time_slots'])} date(s) "
                            f"from {response.url}"
                        )
                except Exception:
                    pass

        page.on("response", on_response)

        logger.info(f"[booksy] Loading {booking_url}")
        await page.goto(booking_url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)

        # Click "Book" next to the matching service
        service_hints = [event_name, "Men's Hair Cut", "Haircut", "Hair Cut", "Book"]
        for service_hint in service_hints:
            try:
                section = (
                    page.locator("*")
                    .filter(has_text=service_hint)
                    .filter(has=page.get_by_text("Book", exact=False))
                    .last
                )
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
            for nav_txt in ["Next week", "Next", "\u203a", ">"]:
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


def _entries_to_slots(
    date_entries: list[dict],
    event_name: str,
    shop_name: str,
    source_url: str,
) -> list[Slot]:
    """Convert Booksy time_slot date entries to normalized Slot objects.

    Each entry: {"date": "YYYY-MM-DD", "slots": [{"t": "HH:MM", "p": ""}, ...]}
    """
    slots: list[Slot] = []
    seen: set[str] = set()

    for entry in date_entries:
        date_str = entry.get("date", "")
        raw_slots = entry.get("slots", [])
        if not date_str or not raw_slots:
            continue

        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        except Exception:
            continue

        for raw in raw_slots:
            t = raw.get("t", "")
            if not t:
                continue
            try:
                hour, minute = map(int, t.split(":"))
                dt = date_obj.replace(hour=hour, minute=minute)
                dt_local = dt.astimezone()

                key = dt_local.isoformat()
                if key in seen:
                    continue
                seen.add(key)

                slots.append(Slot(
                    shop=shop_name,
                    service=event_name,
                    provider=None,
                    start_time=dt_local,
                    end_time=None,
                    duration_min=None,
                    source_url=source_url,
                    platform="booksy",
                ))
            except Exception:
                continue

    slots.sort(key=lambda s: s.start_time)
    return slots


def scrape_booksy_slots(
    booking_url: str,
    event_name: str = "Haircut",
    preferences: str = "",
    shop_name: str = "",
) -> list[Slot]:
    """Main entry point -- returns normalized Slot objects."""
    try:
        date_entries = asyncio.run(
            _intercept_time_slots(booking_url, event_name)
        )
        logger.info(f"[booksy] Total date entries captured: {len(date_entries)}")
        if not date_entries:
            return []
        return _entries_to_slots(date_entries, event_name, shop_name, booking_url)
    except Exception as e:
        logger.exception("[booksy] Scraper failed")
        return []
