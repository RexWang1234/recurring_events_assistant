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


def get_shops(ev_config: dict) -> list[dict]:
    """Normalize event config to a list of shops.

    Supports both formats:
      New:  shops: [{name: "...", url: "..."}]
      Old:  shop_name: "...", booking_url: "..."
    """
    if "shops" in ev_config and ev_config["shops"]:
        return ev_config["shops"]
    # Backward compat: single shop from old fields
    if "booking_url" in ev_config:
        return [{
            "name": ev_config.get("shop_name", ""),
            "url": ev_config["booking_url"],
        }]
    return []


def fetch_and_filter_slots(
    booking_url: str,
    event_name: str,
    preferences_text: str,
    shop_name: str = "",
) -> dict:
    """Fetch and filter slots from a single shop URL.

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


def fetch_slots_multi_shop(
    shops: list[dict],
    event_name: str,
    preferences_text: str,
    shop_filter: str = "",
) -> dict:
    """Fetch and filter slots across multiple shops for one event.

    If shop_filter is set, only check that shop. Otherwise check all.

    Returns {
        "results": [{shop_name, booking_url, slots, message, platform}, ...],
        "total_slots": int,
    }
    """
    pref = parse_preferences(preferences_text)
    results = []

    for shop in shops:
        shop_name = shop["name"]
        booking_url = shop["url"]

        # If user specified a shop, skip others
        if shop_filter and shop_filter.lower() not in shop_name.lower():
            continue

        platform = detect_platform(booking_url)
        raw_slots = _fetch_raw_slots(
            booking_url, event_name, preferences_text, platform, shop_name
        )

        # Filter
        filtered = [s for s in raw_slots if pref.matches(s)] if raw_slots else []
        message = ""
        if not filtered and raw_slots:
            filtered = raw_slots
            message = f"No slots matched preferences ({preferences_text}). Showing all:"

        reverse = pref.sort_order == "latest"
        filtered.sort(key=lambda s: s.start_time, reverse=reverse)

        results.append({
            "shop_name": shop_name,
            "booking_url": booking_url,
            "slots": filtered[:8],
            "message": message,
            "platform": platform,
        })

    total = sum(len(r["slots"]) for r in results)
    return {"results": results, "total_slots": total}


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


def format_multi_shop_results(results: list[dict]) -> str:
    """Format results from multiple shops into a string for the agent."""
    if not results:
        return "No shops configured for this event."

    sections = []
    for r in results:
        shop = r["shop_name"]
        url = r["booking_url"]
        slots = r["slots"]
        msg = r["message"]

        if not slots:
            sections.append(f"{shop}: No available slots found.\nBook manually: {url}")
            continue

        lines = [f"{i}. {s.to_display()}" for i, s in enumerate(slots, 1)]
        section = f"{shop}:\n" + "\n".join(lines)
        if msg:
            section = f"{shop}: {msg}\n" + "\n".join(lines)
        section += f"\nBook here: {url}"
        sections.append(section)

    return "\n\n".join(sections)
