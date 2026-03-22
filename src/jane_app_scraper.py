"""
Jane App availability scraper.

Uses Playwright to establish a session and intercept the internal
/api/v2/openings/for_discipline API response — no text parsing, no AI,
no screenshots. Returns clean structured slot data.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright, Response

logger = logging.getLogger(__name__)

WEEKS_TO_SCAN = 3


async def _get_openings(booking_url: str, preferences: str) -> list[dict]:
    """
    Navigate Jane App, click the massage service, intercept the openings API
    response, and return the raw opening objects.
    """
    openings = []

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

        # Intercept the openings API response
        captured = []

        async def on_response(response: Response):
            if "openings/for_discipline" in response.url and response.status == 200:
                try:
                    data = await response.json()
                    captured.append(data)
                    logger.info(f"[jane] Captured openings response: {response.url}")
                except Exception:
                    pass

        page.on("response", on_response)

        logger.info(f"[jane] Loading {booking_url}")
        await page.goto(booking_url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1000)

        # Click the Massage Therapy / Registered Massage link
        for kw in ["Registered Massage", "Massage Therapy", "Massage"]:
            try:
                el = page.get_by_text(kw, exact=False).first
                if await el.is_visible(timeout=3000):
                    await el.click()
                    logger.info(f"[jane] Clicked: '{kw}'")
                    # Wait for the openings API to fire
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                continue

        # If we got the first week, navigate forward to get more weeks
        if captured:
            for week in range(1, WEEKS_TO_SCAN):
                try:
                    next_date = (
                        datetime.now(timezone.utc) + timedelta(weeks=week)
                    ).strftime("%Y-%m-%d")

                    # Wait for the next response after navigating
                    async with page.expect_response(
                        lambda r: "openings/for_discipline" in r.url and r.status == 200,
                        timeout=5000,
                    ) as resp_info:
                        # Click "Next 7 Days" button
                        for btn_text in ["Next 7 Days", "Next", "›", ">"]:
                            try:
                                btn = page.get_by_text(btn_text, exact=False).first
                                if await btn.is_visible(timeout=2000):
                                    await btn.click()
                                    break
                            except Exception:
                                continue

                    response = await resp_info.value
                    data = await response.json()
                    captured.append(data)
                    logger.info(f"[jane] Captured week {week + 1} openings")
                except Exception as e:
                    logger.debug(f"[jane] Could not get week {week + 1}: {e}")
                    break

        await browser.close()

        # Flatten all opening objects from all captured responses
        for data in captured:
            if isinstance(data, list):
                openings.extend(data)
            elif isinstance(data, dict):
                # Common wrapper patterns
                for key in ("openings", "data", "slots", "appointments"):
                    if key in data and isinstance(data[key], list):
                        openings.extend(data[key])
                        break
                else:
                    # Maybe the dict itself is one opening
                    if "time" in data or "start_at" in data or "start" in data:
                        openings.append(data)

    return openings


def _parse_openings(openings: list[dict], preferences: str) -> list[str]:
    """Convert raw opening dicts into human-readable slot strings.

    Jane App openings schema:
      start_at: ISO datetime string
      end_at:   ISO datetime string
      duration: integer in SECONDS (e.g. 2700 = 45 min, 3600 = 60 min)
      staff_member_id: integer (no name in this endpoint)
      status: "opening"
    """
    slots = []
    seen = set()

    # Preference: preferred duration in minutes
    pref_lower = preferences.lower()
    preferred_duration_min = None
    for dur in ["90", "75", "60", "45", "30"]:
        if dur in pref_lower:
            preferred_duration_min = int(dur)
            break

    for opening in openings:
        start = opening.get("start_at")
        if not start:
            continue

        # duration is in seconds → convert to minutes
        duration_sec = opening.get("duration")
        duration_min = int(duration_sec) // 60 if duration_sec else None

        # Note: duration filtering not applied here — the API returns slots for
        # a specific treatment (set by the booking page default). Slot duration
        # is shown in the label so the user can choose appropriately.

        try:
            dt = datetime.fromisoformat(str(start))
            dt_local = dt.astimezone()
            label = dt_local.strftime("%a %b %-d – %-I:%M %p")
            if duration_min:
                label += f" ({duration_min} min)"

            if label not in seen:
                seen.add(label)
                slots.append((dt, label))
        except Exception:
            continue

    # Sort by datetime, return labels only
    slots.sort(key=lambda x: x[0])
    return [label for _, label in slots[:8]]


async def get_jane_availability(booking_url: str, preferences: str = "") -> dict:
    """Main entry point — returns {"slots": [...], "message": "..."}"""
    try:
        openings = await _get_openings(booking_url, preferences)
        logger.info(f"[jane] Total raw openings captured: {len(openings)}")

        if not openings:
            return {
                "slots": [],
                "message": "Could not retrieve availability from the booking site.",
            }

        slots = _parse_openings(openings, preferences)

        if not slots:
            return {
                "slots": [],
                "message": "No available slots found in the next few weeks.",
            }

        return {"slots": slots, "message": ""}

    except Exception as e:
        logger.exception("[jane] Scraper failed")
        return {"slots": [], "message": f"Error checking availability: {e}"}


def get_available_slots_jane(booking_url: str, preferences: str = "") -> dict:
    """Sync wrapper."""
    return asyncio.run(get_jane_availability(booking_url, preferences))
