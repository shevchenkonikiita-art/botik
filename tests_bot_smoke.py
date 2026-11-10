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
    _apply_detail_text,
    _clean_location,
    _fill_card_facts,
    _stamp_posted_from_card,
    format_posted_dt,
    is_fresh_for_watch,
    listing_from_snapshot,
    listing_matches_user,
    listing_posted_within,
    listing_rent_czynsz,
    listing_snapshot,
    parse_card_posted,
    parse_czynsz_zl,
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


def test_budget_rent_plus_czynsz() -> None:
    item = Listing(
        id="otodom:demo",
        title="Apartament 47m, Grunwald, Ptasia 20, nowe, garaż",
        price="3100 zł",
        location="Grunwald, Poznań",
        area="47 m²",
        url="https://www.otodom.pl/pl/oferta/x",
        rent_zl=3100,
        czynsz_zl=850,
        area_m2=47.0,
        rooms_hint=2,
        owner_hint="private",
        furnished="tak",
        district="Grunwald",
    )
    cap = item.format_caption()
    assert "3 100 zł + czynsz 850 = 3 950 zł" in cap
    assert "≈ 84 zł/m²" in cap
    tight = {**DEFAULT_FILTERS, "max_price": 3100, "setup_done": True}
    ok = {**DEFAULT_FILTERS, "min_price": 3500, "max_price": 5000, "setup_done": True}
    assert not listing_matches_user(item, tight)
    assert listing_matches_user(item, ok)
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    posted = now.replace(second=0, microsecond=0) - timedelta(minutes=20)
    today = Listing(
        id="olx:today",
        title="nowe mieszkanie",
        price="2000 zł",
        location="Poznań, Wilda",
        area="30 m²",
        url="https://www.olx.pl/x",
        raw_text="Dodane dzisiaj",
        extras={
            "posted_iso": posted.isoformat(),
            "posted_has_time": True,
        },
    )
    assert is_fresh_for_watch(today, 1)
    assert f"Додано о {posted:%H:%M}" in today.format_caption()


def test_czynsz_not_doubled_from_title() -> None:
    assert parse_czynsz_zl("Kawalerka Grunwald Czynsz 1900!") is None
    assert parse_czynsz_zl("Czynsz (dodatkowo): 400 zł") == 400
    assert parse_czynsz_zl("+ czynsz: 929 zł/miesiąc") == 929
    assert parse_czynsz_zl("czynsz administracyjny: 850 zł") == 850
    assert parse_czynsz_zl("Czynsz: 720 zł") == 720
    assert parse_czynsz_zl("czynsz najmu: 2500 zł") is None

    doubled = Listing(
        id="olx:double",
        title="Kawalerka Grunwald Czynsz 1900!",
        price="1900 zł",
        location="Poznań, Grunwald",
        area="28 m²",
        url="https://www.olx.pl/d/oferta/x",
        raw_text="Kawalerka Grunwald Czynsz 1900!\n1 900 zł",
    )
    _fill_card_facts(doubled, doubled.raw_text)
    rent, czynsz = listing_rent_czynsz(doubled)
    assert rent == 1900
    assert czynsz == 0

    olx = Listing(
        id="olx:detail",
        title="2 pokoje Wilda",
        price="2950 zł",
        location="Poznań, Wilda",
        area="38 m²",
        url="https://www.olx.pl/d/oferta/y",
        rent_zl=2950,
        raw_text="2 pokoje Wilda\n2 950 zł",
    )
    _apply_detail_text(
        olx,
        "Czynsz (dodatkowo): 400 zł\nPowierzchnia: 38 m²\nLiczba pokoi: 2",
    )
    rent, czynsz = listing_rent_czynsz(olx)
    assert rent == 2950
    assert czynsz == 400
    assert "2 950 zł + czynsz 400 = 3 350 zł" in olx.format_caption()

    otodom = Listing(
        id="otodom:card",
        title="Mieszkanie Jeżyce",
        price="3200 zł",
        location="Jeżyce, Poznań",
        area="42 m²",
        url="https://www.otodom.pl/pl/oferta/x",
        raw_text="Mieszkanie Jeżyce\n3 200 zł\n+ czynsz: 929 zł/miesiąc\n42 m²",
    )
    _fill_card_facts(otodom, otodom.raw_text)
    rent, czynsz = listing_rent_czynsz(otodom)
    assert rent == 3200
    assert czynsz == 929


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
    test_budget_rent_plus_czynsz()
    test_czynsz_not_doubled_from_title()
    test_imports_and_keyboards()
    test_watch_pipeline()
    test_snapshot_roundtrip()
    test_quiet_hours_toggle()
    print("OK: bot smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
