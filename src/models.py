"""Normalized data models for the calendar assistant."""

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional


@dataclass
class Slot:
    """Normalized appointment slot from any booking platform."""
    shop: str
    service: str
    provider: Optional[str]
    start_time: datetime
    end_time: Optional[datetime]
    duration_min: Optional[int]
    source_url: str
    platform: str  # "janeapp", "booksy", "generic"

    def to_display(self) -> str:
        """Human-readable single-line representation."""
        label = self.start_time.strftime("%a %b %-d \u2013 %-I:%M %p")
        if self.duration_min:
            label += f" ({self.duration_min} min)"
        if self.provider:
            label += f" with {self.provider}"
        return label


@dataclass
class TimePreference:
    """Structured time preference parsed from natural language."""
    days: Optional[set[int]] = None    # 0=Mon ... 6=Sun; None = any day
    time_start: Optional[time] = None  # e.g. time(12, 0)
    time_end: Optional[time] = None    # e.g. time(17, 0)
    sort_order: str = "earliest"       # "earliest" or "latest"

    def matches(self, slot: Slot) -> bool:
        """Check if a slot matches this preference."""
        if self.days is not None and slot.start_time.weekday() not in self.days:
            return False
        slot_time = slot.start_time.time()
        if self.time_start and slot_time < self.time_start:
            return False
        if self.time_end and slot_time >= self.time_end:
            return False
        return True
