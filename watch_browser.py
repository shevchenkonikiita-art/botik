"""Один браузер для фонового watch — швидше, як у комерційних rental-ботів."""

from __future__ import annotations

import logging
import threading
from typing import Any

from browser_executor import user_waiting
from config import DEFAULT_FILTERS, WATCH_PAGES
from scraper import (
    _collect_last_days_raw,
    _dedupe,
    _finalize_week_batch,
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
    """«За 7 днів» — один Chromium, коротший скан (Railway)."""
    f = {**DEFAULT_FILTERS, **filters}
    with _lock:
        page = _ensure_page()
        matched = _collect_last_days_raw(page, f, days, max_pages=3)[:25]
        for item in matched[:12]:
            try:
                enrich_on_page(page, item)
            except Exception:
                logger.warning("week enrich skip %s", item.id, exc_info=True)
        return _finalize_week_batch(matched, f, days)


def watch_enrich_many(listings: list) -> list:
    with _lock:
        page = _ensure_page()
        pending = [
            x
            for x in listings
            if not x.details_loaded and "facebook.com" not in (x.url or "")
        ]
        for item in pending:
            if user_waiting():
                logger.info("watch enrich yields to menu button")
                break
            try:
                enrich_on_page(page, item)
            except Exception:
                logger.warning("watch enrich skip %s", item.id, exc_info=True)
        return listings
