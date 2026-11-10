"""Картка оголошення як у Warsaw Rent: альбом фото + точний чинш."""

from __future__ import annotations

import logging
from io import BytesIO

import httpx
from telegram import InputMediaPhoto
from telegram.ext import ContextTypes

import db
from config import DISPLAY_TZ
from handlers.keyboards import listing_keyboard
from scraper import Listing

logger = logging.getLogger(__name__)

PHOTO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    # Без webp/avif — інакше olxcdn віддає WebP, який Telegram відхиляє.
    "Accept": "image/jpeg,image/png,image/*;q=0.8",
    "Referer": "https://www.olx.pl/",
}


def _photo_headers(item: Listing) -> dict[str, str]:
    headers = dict(PHOTO_HEADERS)
    if item.source == "otodom" or "otodom.pl" in (item.url or ""):
        headers["Referer"] = "https://www.otodom.pl/"
    return headers


def _as_photo(content: bytes) -> BytesIO | None:
    """JPEG/PNG для Telegram; WebP конвертуємо в JPEG."""
    if len(content) < 400:
        return None
    if content[:2] == b"\xff\xd8":
        name = "photo.jpg"
        bio = BytesIO(content)
        bio.name = name
        return bio
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        bio = BytesIO(content)
        bio.name = "photo.png"
        return bio
    if len(content) > 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        try:
            from PIL import Image

            img = Image.open(BytesIO(content)).convert("RGB")
            out = BytesIO()
            img.save(out, format="JPEG", quality=88)
            out.seek(0)
            out.name = "photo.jpg"
            return out
        except Exception:
            logger.debug("webp convert failed", exc_info=True)
            return None
    return None


async def _download_photo(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> BytesIO | None:
    try:
        resp = await client.get(url, headers=headers or PHOTO_HEADERS)
        if resp.status_code != 200:
            return None
        kind = resp.headers.get("content-type", "")
        if "svg" in kind or "text" in kind:
            return None
        return _as_photo(resp.content)
    except Exception:
        logger.debug("photo download failed %s", url, exc_info=True)
        return None


async def send_listing(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    item: Listing,
    *,
    prefix: str = "",
    display_tz: str | None = None,
    date_lang: str = "uk",
) -> None:
    display_tz = display_tz or db.get_filters(chat_id).get("display_tz") or DISPLAY_TZ
    markup = listing_keyboard(item.url, item.maps_url())
    from scraper import _normalize_img_url

    raw_urls = list(item.photos or [])
    if item.photo_url:
        raw_urls.insert(0, item.photo_url)
    urls: list[str] = []
    seen: set[str] = set()
    for raw in raw_urls:
        url = _normalize_img_url(str(raw or ""))
        if not url.startswith("http"):
            continue
        key = url.split(";")[0].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)

    headers = _photo_headers(item)
    files: list[BytesIO] = []
    if urls:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            for url in urls[:4]:
                photo = await _download_photo(client, url, headers)
                if photo is None:
                    alt = dict(headers)
                    alt["Referer"] = (
                        "https://www.otodom.pl/"
                        if "olx.pl" in headers.get("Referer", "")
                        else "https://www.olx.pl/"
                    )
                    photo = await _download_photo(client, url, alt)
                if photo is not None:
                    files.append(photo)

    # Альбом у Telegram не підтримує inline-кнопки — лінки в підписі.
    use_album = len(files) >= 2
    caption = (
        (
            prefix
            + item.format_caption(
                embed_links=use_album, display_tz=display_tz, date_lang=date_lang
            )
        )
        if prefix
        else item.format_caption(
            embed_links=use_album, display_tz=display_tz, date_lang=date_lang
        )
    )[:1024]

    if use_album:
        media = []
        for i, bio in enumerate(files):
            if i == 0:
                media.append(
                    InputMediaPhoto(
                        media=bio,
                        caption=caption,
                        parse_mode="HTML",
                    )
                )
            else:
                media.append(InputMediaPhoto(media=bio))
        try:
            await context.bot.send_media_group(chat_id=chat_id, media=media)
            return
        except Exception:
            logger.exception("media group failed for %s", item.id)

    caption = (
        (
            prefix
            + item.format_caption(
                embed_links=False, display_tz=display_tz, date_lang=date_lang
            )
        )
        if prefix
        else item.format_caption(
            embed_links=False, display_tz=display_tz, date_lang=date_lang
        )
    )[:1024]

    if files:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=files[0],
                caption=caption,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return
        except Exception:
            logger.exception("single photo failed for %s", item.id)

    await context.bot.send_message(
        chat_id=chat_id,
        text=caption,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=markup,
    )
