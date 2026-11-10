"""Один браузер для фонового watch — швидше, як у комерційних rental-ботів."""

from __future__ import annotations

import logging
import threading
from typing import Any

from browser_executor import user_waiting
from config import DEFAULT_FILTERS, WATCH_PAGES
from scraper import (
    _dedupe,
    _open_browser,
    enrich_on_page,
    fetch_olx_listings,
    fetch_otodom_listings,
    fill_missing_czynsz,
    listing_matches_user,
)

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_playwright = None
_browser = None
_page = None


def _reset_unlocked() -> None:
    global _playwright, _browser, _page
    try:
        if _browser:
            _browser.close()
    except Exception:
        pass
    try:
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    _playwright = _browser = _page = None


def _ensure_page():
    global _playwright, _browser, _page
    if _page is not None:
        try:
            if not _page.is_closed():
                return _page
        except Exception:
            pass
    _reset_unlocked()
    _playwright, _browser, _page = _open_browser()
    return _page


def user_search_listings(filters: dict[str, Any], limit: int = 15) -> list:
    """Меню «Показати зараз» — той самий браузер, що й watch."""
    f = {**DEFAULT_FILTERS, **filters}
    with _lock:
        page = _ensure_page()
        collected: list = []
        for fetcher in (fetch_olx_listings, fetch_otodom_listings):
            try:
                collected.extend(
                    fetcher(f, max(limit, 12), page, page_num=1, for_watch=True)
                )
            except Exception:
                logger.exception("user search fetch failed, reset browser")
                _reset_unlocked()
                page = _ensure_page()
                collected.extend(
                    fetcher(f, max(limit, 12), page, page_num=1, for_watch=True)
                )
        collected = _dedupe(collected, limit * 3)
        fill_missing_czynsz(page, collected, limit=min(12, len(collected)))
        matched = [x for x in collected if listing_matches_user(x, f)]
        return matched[: limit * 2]


def watch_fetch_listings(filters: dict[str, Any], per_source: int) -> list:
    """Повний верх стрічки LATEST (усі картки на сторінці), без раннього limit у фільтрі."""
    with _lock:
        page = _ensure_page()
        collected: list = []
        for page_num in range(1, WATCH_PAGES + 1):
            if user_waiting():
                logger.info("watch fetch yields to menu button")
                break
            for fetcher in (fetch_olx_listings, fetch_otodom_listings):
                try:
                    collected.extend(
                        fetcher(
                            filters,
                            per_source,
                            page,
                            page_num=page_num,
                            for_watch=True,
                        )
                    )
                except Exception:
                    logger.exception(
                        "watch fetch failed (page %s), reset browser",
                        page_num,
                    )
                    _reset_unlocked()
                    page = _ensure_page()
                    collected.extend(
                        fetcher(
                            filters,
                            per_source,
                            page,
                            page_num=page_num,
                            for_watch=True,
                        )
                    )
        return _dedupe(collected)


def _page_older_than(batch: list, days: int) -> bool:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from scraper import listing_posted_at

    moments = [listing_posted_at(x) for x in batch]
    dated = [m for m in moments if m is not None]
    if len(dated) < 4:
        return False
    cutoff = datetime.now(ZoneInfo("Europe/Warsaw")) - timedelta(days=days)
    return all(m < cutoff for m in dated)


def watch_week_page(
    filters: dict[str, Any], page_num: int, days: int = 7
) -> tuple[list, bool]:
    """Одна сторінка OLX + Otodom. Повертає (картки, чи далі вже старіше 7 днів)."""
    from scraper import listing_matches_user, listing_posted_within

    f = {**DEFAULT_FILTERS, **filters}
    with _lock:
        page = _ensure_page()
        collected: list = []
        older = True
        any_batch = False
        for fetcher in (fetch_olx_listings, fetch_otodom_listings):
            try:
                batch = fetcher(f, 36, page, page_num=page_num, for_watch=True)
            except Exception:
                logger.exception("week page failed, reset")
                _reset_unlocked()
                page = _ensure_page()
                try:
                    batch = fetcher(f, 36, page, page_num=page_num, for_watch=True)
                except Exception:
                    logger.exception("week page retry failed")
                    batch = []
            if batch:
                any_batch = True
            if not _page_older_than(batch, days):
                older = False
            for item in batch:
                if not listing_matches_user(item, f):
                    continue
                if not listing_posted_within(item, days):
                    continue
                collected.append(item)
        stop = (not any_batch) or (page_num >= 2 and older)
        return _dedupe(collected), stop


def watch_week_listings(filters: dict[str, Any], days: int = 7) -> list:
    """Архів за 7 днів з карток списку (фото+дата), без відкриття кожного оголошення."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from scraper import listing_posted_at, listing_posted_within

    kept: list = []
    for page_num in range(1, 5):
        batch, stop = watch_week_page(filters, page_num, days)
        kept.extend(batch)
        if stop or len(_dedupe(kept)) >= 30:
            break
    fresh = []
    seen = set()
    for item in _dedupe(kept):
        if item.id in seen:
            continue
        if not listing_posted_within(item, days):
            continue
        if item.photo_url and not item.photos:
            item.photos = [item.photo_url]
        seen.add(item.id)
        fresh.append(item)
        if len(fresh) >= 30:
            break
    late = datetime.max.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
    fresh.sort(key=lambda x: listing_posted_at(x) or late)
    f = {**DEFAULT_FILTERS, **filters}
    with _lock:
        page = _ensure_page()
        fill_missing_czynsz(page, fresh, limit=min(16, len(fresh)))
    fresh = [x for x in fresh if listing_matches_user(x, f)]
    logger.info("week archive oldest→newest: %s", len(fresh))
    return fresh


def watch_enrich_many(listings: list, yield_to_user: bool = True) -> list:
    with _lock:
        page = _ensure_page()
        pending = [
            x
            for x in listings
            if not x.details_loaded and "facebook.com" not in (x.url or "")
        ]
        for item in pending:
            if yield_to_user and user_waiting():
                logger.info("watch enrich yields to menu button")
                break
            try:
                enrich_on_page(page, item)
            except Exception:
                logger.warning("watch enrich skip %s", item.id, exc_info=True)
        return listings
