"""
Booking agent -- thin routing layer.

Routes availability requests to platform-specific scrapers via slot_service.
The old AI browser agent (Claude + Playwright) has been removed in favor of
deterministic API interception scrapers.
"""

import asyncio
from src.slot_service import fetch_and_filter_slots, format_slots_for_display


def get_available_slots(
    booking_url: str,
    event_name: str,
    preferences: str,
    shop_name: str = "",
) -> dict:
    """Return formatted availability results.

    Delegates to slot_service which handles:
      1. Platform detection and scraper routing
      2. Normalized Slot objects from scrapers
      3. Deterministic preference filtering
      4. Sorting and limiting

    Returns {"display": str, "slots": list[Slot], "booking_url": str}
    """
    result = fetch_and_filter_slots(booking_url, event_name, preferences, shop_name)
    display = format_slots_for_display(
        result["slots"], result["booking_url"], result["message"]
    )
    return {
        "display": display,
        "slots": result["slots"],
        "booking_url": result["booking_url"],
        "platform": result["platform"],
    }
