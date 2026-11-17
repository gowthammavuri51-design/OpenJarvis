"""Understand reminder requests like "remind me at 6pm to call mom".

Supported time phrases (case-insensitive, local time):

* ``in 30 min`` / ``in 2 hours`` / ``in 1 day`` / ``in 45 seconds``
* ``at 6pm`` / ``at 18:30`` / ``at 7:15 am``  (today, or tomorrow if passed)
* ``tomorrow at 9am`` / ``tomorrow`` (= 09:00 tomorrow)
* ``every day at 8am`` / ``daily at 8:00``  (repeats every day)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional

_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}  # fmt: skip

_CLOCK = r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm|a\.m\.|p\.m\.)?"
_IN = r"in\s+(?P<n>\d+)\s*(?P<unit>[a-z]+)"
# Each form is matched separately: named groups cannot repeat in one regex.
_RE_IN = re.compile(rf"^{_IN}$", re.I)
_RE_DAILY = re.compile(rf"^(?:every\s*day|daily)\s+at\s+{_CLOCK}$", re.I)
_RE_TOMORROW = re.compile(rf"^tomorrow(?:\s+at\s+{_CLOCK})?$", re.I)
_RE_AT = re.compile(rf"^at\s+{_CLOCK}$", re.I)

_WHEN_ANY = (
    r"(?:in\s+\d+\s*[a-z]+"
    r"|(?:every\s*day|daily)\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?"
    r"|tomorrow(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)?"
    r"|at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)"
)
_RE_WHEN_FIRST = re.compile(
    rf"^remind\s+me\s+(?P<when>{_WHEN_ANY})\s+(?:to\s+|about\s+)?(?P<what>.+)$",
    re.I | re.S,
)
_RE_WHAT_FIRST = re.compile(
    rf"^remind\s+me\s+(?:to\s+|about\s+)?(?P<what>.+?)\s+(?P<when>{_WHEN_ANY})\s*[.!]?$",
    re.I | re.S,
)


@dataclass
class When:
    due: datetime
    daily: bool = False


def _clock(h: str, m: Optional[str], ampm: Optional[str]) -> time:
    hour, minute = int(h), int(m or 0)
    suffix = (ampm or "").lower().replace(".", "")
    if suffix:
        if not 1 <= hour <= 12:
            raise ValueError(f"{h} is not a valid 12-hour time")
        hour = hour % 12 + (12 if suffix == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("invalid time of day")
    return time(hour, minute)


def _next_at(now: datetime, t: time) -> datetime:
    due = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    return due if due > now else due + timedelta(days=1)


def parse_when(phrase: str, now: datetime) -> When:
    """Turn a time phrase into a due datetime. Raises ValueError if unclear."""
    p = " ".join((phrase or "").strip().rstrip(".!").split())
    m = _RE_IN.match(p)
    if m:
        unit = _UNITS.get(m.group("unit").lower())
        if unit is None:
            raise ValueError(f"unknown time unit {m.group('unit')!r}")
        seconds = int(m.group("n")) * unit
        if seconds <= 0:
            raise ValueError("the delay must be positive")
        return When(now + timedelta(seconds=seconds))
    m = _RE_DAILY.match(p)
    if m:
        t = _clock(m.group("h"), m.group("m"), m.group("ampm"))
        return When(_next_at(now, t), daily=True)
    m = _RE_TOMORROW.match(p)
    if m:
        t = (
            _clock(m.group("h"), m.group("m"), m.group("ampm"))
            if m.group("h")
            else time(9, 0)
        )
        base = (now + timedelta(days=1)).replace(
            hour=t.hour, minute=t.minute, second=0, microsecond=0
        )
        return When(base)
    m = _RE_AT.match(p)
    if m:
        t = _clock(m.group("h"), m.group("m"), m.group("ampm"))
        return When(_next_at(now, t))
    raise ValueError(
        "I couldn't understand the time. Try 'in 30 min', 'at 6pm', "
        "'tomorrow at 9am' or 'every day at 8am'."
    )


def parse_reminder_request(message: str, now: datetime) -> Optional[tuple[When, str]]:
    """Parse "remind me ..." messages. Returns None if it isn't one.

    Raises ValueError when it *is* a reminder request but the time is unclear.
    """
    msg = " ".join((message or "").strip().split())
    if not re.match(r"^remind\s+me\b", msg, re.I):
        return None
    for rx in (_RE_WHEN_FIRST, _RE_WHAT_FIRST):
        m = rx.match(msg)
        if m:
            what = m.group("what").strip().rstrip(".!")
            if what.lower().startswith(("to ", "about ")):
                what = what.split(" ", 1)[1]
            if what:
                return parse_when(m.group("when"), now), what
    raise ValueError(
        "Tell me when, e.g. 'remind me at 6pm to call mom' or "
        "'remind me in 20 min to check the oven'."
    )


def describe(due: datetime, now: datetime, daily: bool = False) -> str:
    """Human-friendly time, e.g. 'today 18:00', 'tomorrow 09:00', 'every day 08:00'."""
    hhmm = due.strftime("%H:%M")
    if daily:
        return f"every day {hhmm}"
    days = (due.date() - now.date()).days
    if days == 0:
        return f"today {hhmm}"
    if days == 1:
        return f"tomorrow {hhmm}"
    return due.strftime("%a %d %b %H:%M")
