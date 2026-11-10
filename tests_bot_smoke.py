"""Автоперевірка бота без ручного Telegram (дозволено запускати самому)."""
from __future__ import annotations

import asyncio
import sys

import db
from config import DEFAULT_FILTERS
from filters_util import merge_filters_for_watch
from handlers.keyboards import main_menu_keyboard, quiet_keyboard, timezone_keyboard
from handlers.texts import home_text, welcome_text
from notify_util import in_quiet_hours
from scraper import (
    Listing,
    _clean_location,
    _stamp_posted_from_card,
    format_posted_dt,
    listing_from_snapshot,
    listing_matches_user,
    listing_posted_within,
    listing_snapshot,
    parse_card_posted,
)
from watch_browser import watch_fetch_listings


def test_card_dates() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 11, 9, 18, 0, tzinfo=ZoneInfo("Europe/Warsaw"))

    moment, has_time = parse_card_posted(
        "Poznań, Grunwald - 11 października 2026", now=now
    )
    assert moment is not None
    assert moment.day == 11 and moment.month == 10
    assert has_time is False
    assert "00:00" not in format_posted_dt(moment, show_time=has_time)

    moment, has_time = parse_card_posted(
        "Poznań, Łacina - Odświeżono dnia 02 listopada 2026", now=now
    )
    assert moment is not None and moment.day == 2 and moment.month == 11
    assert has_time is False

    moment, has_time = parse_card_posted("Dodane wczoraj\n3000 zł", now=now)
    assert moment is not None and moment.day == 8
    assert has_time is False

    fresh = Listing(
        id="olx:fresh",
        title="2 pokoje",
        price="2400 zł",
        location="Poznań, Stare Miasto - 09 listopada 2026",
        area="48 m²",
        url="https://www.olx.pl/d/oferta/x",
        raw_text="2 pokoje\n2400 zł\nPoznań, Stare Miasto - 09 listopada 2026",
    )
    _stamp_posted_from_card(fresh, fresh.raw_text)
    assert listing_posted_within(fresh, 7)
    assert "00:00" not in (fresh.posted_label or "")
    assert "00:00" not in fresh.format_caption()

    old = Listing(
        id="olx:old",
        title="stare",
        price="2800 zł",
        location="Poznań, Grunwald - 11 października 2026",
        area="55 m²",
        url="https://www.olx.pl/d/oferta/y",
        raw_text="stare\n2800 zł\nPoznań, Grunwald - 11 października 2026",
    )
    _stamp_posted_from_card(old, old.raw_text)
    assert not listing_posted_within(old, 7)
    assert _clean_location(old.location) == "Poznań, Grunwald"


def test_imports_and_keyboards() -> None:
    db.init_db()
    f = {**DEFAULT_FILTERS, "setup_done": True}
    assert main_menu_keyboard(True).inline_keyboard
    assert quiet_keyboard(f).inline_keyboard
    assert timezone_keyboard(f).inline_keyboard
    assert "FindHome" in welcome_text()
    assert "OLX" in home_text(f)


def test_watch_pipeline() -> None:
    users = db.list_active_users()
    merged = merge_filters_for_watch([db.get_filters(u) for u in users] or [{**DEFAULT_FILTERS}])
    items = watch_fetch_listings(merged, 12)
    assert len(items) >= 5, f"too few listings: {len(items)}"
    if users:
        f = db.get_filters(users[0])
        hits = [x for x in items if listing_matches_user(x, f)]
        assert hits, "no listings match active user filters on feed top"


def test_snapshot_roundtrip() -> None:
    users = db.list_active_users()
    merged = merge_filters_for_watch([db.get_filters(u) for u in users] or [{**DEFAULT_FILTERS}])
    items = watch_fetch_listings(merged, 3)
    if not items:
        return
    snap = listing_snapshot(items[0])
    restored = listing_from_snapshot(snap)
    assert restored.id == items[0].id
    assert restored.url == items[0].url


def test_quiet_hours_toggle() -> None:
    f = {**DEFAULT_FILTERS, "quiet_hours": False}
    assert not in_quiet_hours(f)
    f["quiet_hours"] = True
    f["quiet_from"] = "23:00"
    f["quiet_to"] = "07:00"
    # result depends on clock — just must not crash
    _ = in_quiet_hours(f)


def main() -> int:
    test_card_dates()
    test_imports_and_keyboards()
    test_watch_pipeline()
    test_snapshot_roundtrip()
    test_quiet_hours_toggle()
    print("OK: bot smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
