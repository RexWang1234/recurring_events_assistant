"""
Jane App availability scraper.

Uses Playwright to establish a session and intercept the internal
/api/v2/openings/for_discipline API response -- no text parsing, no AI,
no screenshots. Returns normalized Slot objects.
"""

import asyncio
import logging
from datetime import datetime

from playwright.async_api import async_playwright, Response

from src.models import Slot

logger = logging.getLogger(__name__)

WEEKS_TO_SCAN = 3


def _service_keywords(event_name: str, preferences: str) -> list[str]:
    """Return ordered list of text labels to try clicking on the Jane App booking page."""
    combined = (event_name + " " + preferences).lower()

    if "massage" in combined:
        return ["Registered Massage", "Massage Therapy", "Massage"]
    elif "chiro" in combined:
        return ["Chiropractic", "Chiropractor", "Chiro"]
    elif "acupuncture" in combined or "acupunct" in combined:
        return ["Acupuncture", "Acupuncturist"]
    elif "physio" in combined:
        return ["Physiotherapy", "Physiotherapist", "Physio"]
    elif "naturo" in combined:
        return ["Naturopathic", "Naturopath", "Naturopathic Doctor"]
    elif "osteo" in combined:
        return ["Osteopathy", "Osteopath"]
    elif "kinesio" in combined or "kinesiolog" in combined:
        return ["Kinesiology", "Kinesiologist"]
    else:
        return [event_name, event_name.title(), event_name.upper()]


async def _get_openings(
    booking_url: str, event_name: str, preferences: str
) -> tuple[list[dict], dict[int, str], str]:
    """Navigate Jane App, click the appropriate service, intercept the openings API.

    Returns (openings, staff_map, service_clicked).
    """
    openings: list[dict] = []
    staff_map: dict[int, str] = {}
    service_clicked = event_name

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

        captured_openings: list[dict] = []
        captured_staff: list[dict] = []

        async def on_response(response: Response):
            url = response.url
            if response.status != 200 or "janeapp" not in url:
                return
            try:
                if "openings/for_discipline" in url:
                    data = await response.json()
                    captured_openings.append(data)
                    logger.info(f"[jane] Captured openings: {url}")
                elif "staff_members" in url or "practitioners" in url:
                    data = await response.json()
                    captured_staff.append(data)
                    logger.info(f"[jane] Captured staff: {url}")
            except Exception:
                pass

        page.on("response", on_response)

        logger.info(f"[jane] Loading {booking_url}")
        await page.goto(booking_url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1000)

        # Click the appropriate service
        service_keywords = _service_keywords(event_name, preferences)
        logger.info(f"[jane] Trying service keywords: {service_keywords}")
        clicked = False
        for kw in service_keywords:
            try:
                el = page.get_by_text(kw, exact=False).first
                if await el.is_visible(timeout=3000):
                    await el.click()
                    logger.info(f"[jane] Clicked service: '{kw}'")
                    service_clicked = kw
                    await page.wait_for_timeout(3000)
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            logger.warning(
                f"[jane] Could not find service for event='{event_name}', "
                "trying first clickable treatment"
            )

        # Navigate forward weeks to get more availability
        if captured_openings:
            for week in range(1, WEEKS_TO_SCAN):
                try:
                    async with page.expect_response(
                        lambda r: "openings/for_discipline" in r.url
                        and r.status == 200,
                        timeout=5000,
                    ) as resp_info:
                        for btn_text in ["Next 7 Days", "Next", "\u203a", ">"]:
                            try:
                                btn = page.get_by_text(btn_text, exact=False).first
                                if await btn.is_visible(timeout=2000):
                                    await btn.click()
                                    break
                            except Exception:
                                continue

                    response = await resp_info.value
                    data = await response.json()
                    captured_openings.append(data)
                    logger.info(f"[jane] Captured week {week + 1} openings")
                except Exception as e:
                    logger.debug(f"[jane] Could not get week {week + 1}: {e}")
                    break

        await browser.close()

        # Flatten all opening objects
        for data in captured_openings:
            if isinstance(data, list):
                openings.extend(data)
            elif isinstance(data, dict):
                for key in ("openings", "data", "slots", "appointments"):
                    if key in data and isinstance(data[key], list):
                        openings.extend(data[key])
                        break
                else:
                    if "time" in data or "start_at" in data or "start" in data:
                        openings.append(data)

        # Build staff ID -> name map
        for data in captured_staff:
            entries = []
            if isinstance(data, list):
                entries = data
            elif isinstance(data, dict):
                for key in ("staff_members", "practitioners", "data", "staff"):
                    if key in data and isinstance(data[key], list):
                        entries = data[key]
                        break
            for entry in entries:
                sid = entry.get("id")
                name = (
                    entry.get("full_name")
                    or entry.get("name")
                    or entry.get("display_name")
                )
                if sid and name:
                    staff_map[int(sid)] = name

        logger.info(f"[jane] Staff map: {staff_map}")

    return openings, staff_map, service_clicked


def _openings_to_slots(
    openings: list[dict],
    staff_map: dict[int, str],
    service: str,
    shop_name: str,
    source_url: str,
) -> list[Slot]:
    """Convert raw Jane App opening dicts into normalized Slot objects.

    Jane App openings schema:
      start_at: ISO datetime
      end_at:   ISO datetime
      duration: integer in SECONDS (e.g. 2700 = 45 min)
      staff_member_id: integer
    """
    slots: list[Slot] = []
    seen: set[str] = set()

    for opening in openings:
        start = opening.get("start_at")
        if not start:
            continue

        try:
            dt = datetime.fromisoformat(str(start))
            dt_local = dt.astimezone()
        except Exception:
            continue

        duration_sec = opening.get("duration")
        duration_min = int(duration_sec) // 60 if duration_sec else None

        end_str = opening.get("end_at")
        end_time = None
        if end_str:
            try:
                end_time = datetime.fromisoformat(str(end_str)).astimezone()
            except Exception:
                pass

        staff_id = opening.get("staff_member_id")
        provider = staff_map.get(int(staff_id)) if staff_id else None

        # Deduplicate by start time + provider
        key = f"{dt_local.isoformat()}|{provider}"
        if key in seen:
            continue
        seen.add(key)

        slots.append(Slot(
            shop=shop_name,
            service=service,
            provider=provider,
            start_time=dt_local,
            end_time=end_time,
            duration_min=duration_min,
            source_url=source_url,
            platform="janeapp",
        ))

    slots.sort(key=lambda s: s.start_time)
    return slots


def scrape_jane_slots(
    booking_url: str,
    event_name: str = "Massage",
    preferences: str = "",
    shop_name: str = "",
) -> list[Slot]:
    """Main entry point -- returns normalized Slot objects."""
    try:
        openings, staff_map, service = asyncio.run(
            _get_openings(booking_url, event_name, preferences)
        )
        logger.info(
            f"[jane] Total raw openings: {len(openings)}, staff known: {len(staff_map)}"
        )
        if not openings:
            return []
        return _openings_to_slots(
            openings, staff_map, service, shop_name, booking_url
        )
    except Exception as e:
        logger.exception("[jane] Scraper failed")
        return []
