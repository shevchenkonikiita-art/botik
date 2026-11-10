"""Тихі години та черга, як у @thewarsawrentbot (без канал-підписки)."""

from __future__ import annotations

import json
from datetime import datetime, time
from zoneinfo import ZoneInfo

from config import DISPLAY_TZ


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")[:2]
    return time(int(hour), int(minute))


def in_quiet_hours(filters: dict) -> bool:
    if not filters.get("quiet_hours"):
        return False
    tz_name = filters.get("display_tz") or DISPLAY_TZ
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DISPLAY_TZ)
    now = datetime.now(tz).time()
    start = _parse_hhmm(str(filters.get("quiet_from") or "23:00"))
    end = _parse_hhmm(str(filters.get("quiet_to") or "07:00"))
    if start < end:
        return start <= now < end
    return now >= start or now < end
