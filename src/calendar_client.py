"""
Apple Calendar client via osascript.
Finds the last occurrence of a named event and calculates the next due date.
No Google API or iCloud credentials required — reads directly from the local Calendar app.
"""

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _run_osascript(script: str) -> str:
    """Run an AppleScript and return stdout."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and result.stderr:
        # Ignore benign "calendar has no events" errors
        if "Can't get" not in result.stderr:
            raise RuntimeError(f"osascript error: {result.stderr.strip()}")
    return result.stdout.strip()


def _get_events_in_range(days_back: int = 400, days_forward: int = 90) -> list[dict]:
    """
    Return all calendar events in the range [now - days_back, now + days_forward].
    Each dict has: title (str), date (datetime).
    """
    # AppleScript: fetch events in date window, return as "YYYY-MM-DD|Title" lines
    script = f"""
set results to {{}}
set now to current date
set lookbackDate to now - ({days_back} * days)
set lookaheadDate to now + ({days_forward} * days)

set skipCalendars to {{"Canadian Holidays", "Birthdays", "Siri Suggestions", "Scheduled Reminders"}}

tell application "Calendar"
    repeat with c in every calendar
        set calName to name of c
        if skipCalendars contains calName then
            -- skip noisy/irrelevant calendars
        else
        try
            set eventsInRange to (every event of c whose start date >= lookbackDate and start date <= lookaheadDate)
            repeat with e in eventsInRange
                set evTitle to (summary of e) as string
                set evStart to start date of e
                set yr to year of evStart as integer
                set mo to month of evStart as integer
                set dy to day of evStart as integer

                if mo < 10 then
                    set moStr to "0" & (mo as string)
                else
                    set moStr to mo as string
                end if
                if dy < 10 then
                    set dyStr to "0" & (dy as string)
                else
                    set dyStr to dy as string
                end if

                set dateStr to (yr as string) & "-" & moStr & "-" & dyStr
                set end of results to dateStr & "|" & evTitle
            end repeat
        end try
        end if
    end repeat
end tell

set output to ""
repeat with r in results
    set output to output & r & linefeed
end repeat
return output
"""
    raw = _run_osascript(script)
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        date_str, title = line.split("|", 1)
        try:
            dt = datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            events.append({"title": title.strip(), "date": dt})
        except ValueError:
            continue
    return events


def get_last_occurrence(keywords: list[str]) -> datetime | None:
    """Return the datetime of the most recent past event matching any keyword."""
    now = datetime.now(timezone.utc)
    events = _get_events_in_range(days_back=400, days_forward=0)

    matches = []
    for ev in events:
        title_lower = ev["title"].lower()
        if any(kw.lower() in title_lower for kw in keywords):
            if ev["date"] <= now:
                matches.append(ev["date"])

    if not matches:
        return None
    return max(matches)


def get_next_scheduled(keywords: list[str]) -> datetime | None:
    """Return the datetime of the next future event matching any keyword, or None."""
    now = datetime.now(timezone.utc)
    events = _get_events_in_range(days_back=0, days_forward=90)

    matches = []
    for ev in events:
        title_lower = ev["title"].lower()
        if any(kw.lower() in title_lower for kw in keywords):
            if ev["date"] > now:
                matches.append(ev["date"])

    if not matches:
        return None
    return min(matches)


def get_event_status(event: dict) -> dict:
    """
    Returns a dict with:
      - last_occurrence: datetime or None
      - next_due: datetime or None       (last + frequency)
      - days_until_due: int or None
      - next_scheduled: datetime or None (already booked future event)
    """
    keywords = event.get("calendar_search_keywords", [event["calendar_event_name"]])
    frequency_weeks = event["frequency_weeks"]

    last = get_last_occurrence(keywords)
    next_scheduled = get_next_scheduled(keywords)

    if last is None:
        return {
            "last_occurrence": None,
            "next_due": None,
            "days_until_due": None,
            "next_scheduled": next_scheduled,
        }

    next_due = last + timedelta(weeks=frequency_weeks)
    now = datetime.now(timezone.utc)
    if next_due.tzinfo is None:
        next_due = next_due.replace(tzinfo=timezone.utc)

    days_until = (next_due - now).days

    return {
        "last_occurrence": last,
        "next_due": next_due,
        "days_until_due": days_until,
        "next_scheduled": next_scheduled,
    }
