"""
Booking agent -- thin routing layer.

Routes availability requests to platform-specific scrapers via slot_service.
Supports single-shop and multi-shop queries.
"""

from src.slot_service import (
    fetch_and_filter_slots,
    fetch_slots_multi_shop,
    format_slots_for_display,
    format_multi_shop_results,
    get_shops,
)


def get_available_slots(
    booking_url: str,
    event_name: str,
    preferences: str,
    shop_name: str = "",
) -> dict:
    """Return formatted availability for a single shop URL."""
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


def get_available_slots_multi(
    ev_config: dict,
    shop_filter: str = "",
) -> dict:
    """Return formatted availability across all shops for an event.

    If shop_filter is set, only check that shop.
    """
    shops = get_shops(ev_config)
    event_name = ev_config["name"]
    preferences = ev_config.get("booking_preferences", "")

    result = fetch_slots_multi_shop(shops, event_name, preferences, shop_filter)
    display = format_multi_shop_results(result["results"])
    return {
        "display": display,
        "results": result["results"],
        "total_slots": result["total_slots"],
    }
