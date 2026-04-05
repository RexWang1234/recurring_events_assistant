"""Slot service -- orchestration layer for availability checking.

Fetches raw slots from platform scrapers, applies preference filters
deterministically, sorts, and returns the best matches. No LLM involved
(LLM is only used inside the generic sniffer as a last resort).
"""

import logging

from src.models import Slot
from src.preferences import parse_preferences

logger = logging.getLogger(__name__)


def detect_platform(url: str) -> str:
    """Detect booking platform from URL."""
    if "janeapp.com" in url:
        return "janeapp"
    if "booksy.com" in url:
        return "booksy"
    return "generic"


def fetch_and_filter_slots(
    booking_url: str,
    event_name: str,
    preferences_text: str,
    shop_name: str = "",
) -> dict:
    """Main entry point for availability checking.

    1. Routes to the correct platform scraper
    2. Gets back normalized Slot objects
    3. Filters by user preferences (deterministic, no LLM)
    4. Sorts by preference order

    Returns {
        "slots": list[Slot],
        "message": str,
        "booking_url": str,
        "platform": str,
    }
    """
    platform = detect_platform(booking_url)
    pref = parse_preferences(preferences_text)

    raw_slots = _fetch_raw_slots(
        booking_url, event_name, preferences_text, platform, shop_name
    )

    if not raw_slots:
        return {
            "slots": [],
            "message": "No availability data found.",
            "booking_url": booking_url,
            "platform": platform,
        }

    # Deterministic filtering
    filtered = [s for s in raw_slots if pref.matches(s)]

    # If filtering eliminates everything, return unfiltered with a note
    message = ""
    if not filtered and raw_slots:
        filtered = raw_slots
        message = (
            f"No slots matched your preferences ({preferences_text}). "
            "Showing all available:"
        )

    # Deterministic sorting
    reverse = pref.sort_order == "latest"
    filtered.sort(key=lambda s: s.start_time, reverse=reverse)

    return {
        "slots": filtered[:8],
        "message": message,
        "booking_url": booking_url,
        "platform": platform,
    }


def _fetch_raw_slots(
    booking_url: str,
    event_name: str,
    preferences: str,
    platform: str,
    shop_name: str,
) -> list[Slot]:
    """Route to the correct scraper and return normalized Slot objects."""
    if platform == "janeapp":
        from src.jane_app_scraper import scrape_jane_slots
        return scrape_jane_slots(booking_url, event_name, preferences, shop_name)

    if platform == "booksy":
        from src.booksy_scraper import scrape_booksy_slots
        result = scrape_booksy_slots(booking_url, event_name, preferences, shop_name)
        if result:
            return result
        logger.warning(
            "[slot_service] Booksy scraper returned no slots, trying generic"
        )

    from src.generic_api_sniffer import scrape_generic_slots
    return scrape_generic_slots(booking_url, event_name, preferences, shop_name)


def format_slots_for_display(
    slots: list[Slot], booking_url: str, message: str = ""
) -> str:
    """Format normalized slots into a human-readable string for the agent."""
    if not slots:
        return f"No available slots found.\nBook manually: {booking_url}"

    lines = [f"{i}. {slot.to_display()}" for i, slot in enumerate(slots, 1)]
    result = "\n".join(lines)
    if message:
        result = f"{message}\n{result}"
    result += f"\n\nBook here: {booking_url}"
    return result
