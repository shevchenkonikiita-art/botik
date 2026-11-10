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
    with _lock:
        page = _ensure_page()
        collected: list = []
        for fetcher in (fetch_olx_listings, fetch_otodom_listings):
            try:
                collected.extend(fetcher(filters, max(limit, 12), page, page_num=1))
            except Exception:
                logger.exception("user search fetch failed, reset browser")
                _reset_unlocked()
                page = _ensure_page()
                collected.extend(fetcher(filters, max(limit, 12), page, page_num=1))
        return _dedupe(collected, limit * 2)


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


def watch_week_listings(filters: dict[str, Any], days: int = 7) -> list:
    """Перша сторінка OLX + Otodom (найновіші), без відсіву «немає дати» до відкриття картки."""
    from scraper import listing_matches_user, listing_posted_at, listing_posted_within

    f = {**DEFAULT_FILTERS, **filters}
    with _lock:
        page = _ensure_page()
        collected: list = []
        for fetcher in (fetch_olx_listings, fetch_otodom_listings):
            try:
                collected.extend(fetcher(f, 20, page, page_num=1))
            except Exception:
                logger.exception("week fetch failed, reset browser")
                _reset_unlocked()
                page = _ensure_page()
                collected.extend(fetcher(f, 20, page, page_num=1))
        matched = [
            x for x in _dedupe(collected) if listing_matches_user(x, f)
        ][:8]
        for item in matched:
            try:
                enrich_on_page(page, item)
            except Exception:
                logger.warning("week enrich skip %s", item.id, exc_info=True)
        fresh = []
        for item in matched:
            if listing_posted_at(item) is None or listing_posted_within(item, days):
                fresh.append(item)
        logger.info("week button: %s of %s cards", len(fresh), len(matched))
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
