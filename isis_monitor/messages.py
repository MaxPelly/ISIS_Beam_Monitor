"""Shared formatting helpers for notification messages."""
from datetime import datetime
from zoneinfo import ZoneInfo

UK_TZ = ZoneInfo("Europe/London")


def fmt_time(dt: datetime) -> str:
    """Format a datetime in local UK time, e.g. 'Wed 23 Sep 14:05'."""
    return dt.astimezone(UK_TZ).strftime("%a %d %b %H:%M")
