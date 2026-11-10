"""Об'єднання фільтрів кількох користувачів для фонового скану."""

from __future__ import annotations

from typing import Any

from config import DEFAULT_FILTERS


def watch_might_match_user(listing, filters: dict[str, Any]) -> bool:
    """Швидка перевірка до enrich — не втрачати оголошення з чиншом у деталях."""
    from price import analyze_price
    from scraper import (
        _in_poznan,
        _is_apartment,
        _matches_districts,
        _matches_rooms,
        listing_matches_user,
    )

    f = {**DEFAULT_FILTERS, **filters}
    if not f.get("setup_done"):
        return False
    if not _in_poznan(listing) or not _is_apartment(listing):
        return False
    if not _matches_districts(listing, f.get("districts") or []):
        return False
    if not _matches_rooms(listing, list(f.get("rooms") or [])):
        return False
    if listing_matches_user(listing, f):
        return True
    from scraper import listing_rent_czynsz

    max_p = int(f.get("max_price") or 99999)
    rent, _czynsz = listing_rent_czynsz(listing)
    if rent is None:
        info = listing.price_info or analyze_price(listing.price, listing.raw_text)
        listing.price_info = info
        rent = info.base
    # Сайт фільтрує за орендою; чинш додамо після картки/enrich. Оренда > бюджету — ніколи не влізе.
    if rent is None:
        return True
    return rent <= max_p


def merge_filters_for_watch(user_filters: list[dict[str, Any]]) -> dict[str, Any]:
    """Широкий запит на OLX/Otodom: не втратити оголошення під чийсь із профілів."""
    if not user_filters:
        return {**DEFAULT_FILTERS}
    merged: dict[str, Any] = {**DEFAULT_FILTERS}
    mins_price: list[int] = []
    maxs_price: list[int] = []
    mins_area: list[int] = []
    maxs_area: list[int] = []
    rooms: set[int] = set()
    for f in user_filters:
        mins_price.append(int(f.get("min_price") or 0))
        maxs_price.append(int(f.get("max_price") or 99999))
        mins_area.append(int(f.get("min_area") or DEFAULT_FILTERS["min_area"]))
        maxs_area.append(int(f.get("max_area") or DEFAULT_FILTERS["max_area"]))
        for r in f.get("rooms") or []:
            rooms.add(int(r))
    merged["min_price"] = min(mins_price)
    merged["max_price"] = max(maxs_price)
    merged["min_area"] = min(mins_area)
    merged["max_area"] = max(maxs_area)
    merged["rooms"] = sorted(rooms)
    return merged
