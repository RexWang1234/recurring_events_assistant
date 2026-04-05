"""Parse natural language preferences into structured filters."""

from datetime import time
from src.models import TimePreference

_DAY_MAP = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_GROUP_MAP = {
    "weekday": {0, 1, 2, 3, 4},
    "weekend": {5, 6},
}

_TIME_RANGES = {
    "morning": (time(8, 0), time(12, 0)),
    "afternoon": (time(12, 0), time(17, 0)),
    "evening": (time(17, 0), time(21, 0)),
}


def parse_preferences(text: str) -> TimePreference:
    """Convert natural language preferences to structured TimePreference.

    Examples:
        "weekday afternoon"       -> Mon-Fri, 12pm-5pm, earliest
        "Saturday morning"        -> Sat, 8am-12pm, earliest
        "earliest next week"      -> any day, any time, earliest
        "evening, latest"         -> any day, 5pm-9pm, latest
    """
    lower = text.lower()
    days: set[int] = set()
    time_start = None
    time_end = None
    sort_order = "earliest"

    # Day groups first (weekday/weekend)
    for word, day_set in _GROUP_MAP.items():
        if word in lower:
            days |= day_set

    # Then specific days (override groups if both present)
    for word, day_num in _DAY_MAP.items():
        if word in lower:
            days.add(day_num)

    # Time-of-day ranges
    for keyword, (t_start, t_end) in _TIME_RANGES.items():
        if keyword in lower:
            time_start = t_start
            time_end = t_end
            break

    # Sort order
    if "latest" in lower or "last" in lower:
        sort_order = "latest"

    return TimePreference(
        days=days if days else None,
        time_start=time_start,
        time_end=time_end,
        sort_order=sort_order,
    )
