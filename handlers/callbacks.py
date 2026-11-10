"""CallbackQuery handlers: wizard + меню у стилі Warsaw Rent."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import db
from browser_executor import run_browser, user_first_off, user_first_on
from config import BUDGET_RANGES, DISTRICTS
from handlers.keyboards import (
    budget_settings_keyboard,
    budget_wizard_keyboard,
    districts_settings_keyboard,
    districts_wizard_keyboard,
    filters_menu_keyboard,
    main_menu_keyboard,
    najem_keyboard,
    owner_keyboard,
    pets_keyboard,
    quiet_keyboard,
    rooms_settings_keyboard,
    rooms_wizard_keyboard,
    start_keyboard,
    timezone_keyboard,
)
from handlers.send import send_listing
from handlers.texts import (
    filters_menu_text,
    filters_summary,
    home_text,
    welcome_text,
    wizard_budget_text,
    wizard_districts_text,
    wizard_rooms_text,
)
from scraper import (
    Listing,
    listing_matches_user,
    listing_posted_at,
    listing_posted_within,
    listing_record,
)
from watch_browser import user_search_listings, watch_week_listings

logger = logging.getLogger(__name__)


def _home_markup(user_id: int, filters: dict[str, Any]):
    watching = db.is_watching(user_id)
    return home_text(filters, watching=watching), main_menu_keyboard(watching=watching)


async def _safe_answer(query) -> None:
    try:
        await query.answer()
    except BadRequest:
        # query застарів, якщо бот був зайнятий парсингом — ігноруємо
        pass


async def _edit(update: Update, text: str, reply_markup=None) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except BadRequest as exc:
        msg = str(exc).lower()
        if "not modified" in msg:
            return
        # якщо не вийшло відредагувати — надсилаємо нове повідомлення
        try:
            await query.message.reply_text(text, reply_markup=reply_markup)
        except Exception:
            logger.exception("failed to send fallback menu message")


def _keep_if_not_old(item: Listing, days: int = 7) -> bool:
    """Сторінка «найновіші»: без дати лишаємо, з датою — лише якщо не старіша за days."""
    if listing_posted_at(item) is None:
        return True
    return listing_posted_within(item, days)


async def _run_search(filters: dict[str, Any], limit: int = 8) -> list[Listing]:
    listings = await run_browser(user_search_listings, filters, max(limit, 12))
    listings = [
        x for x in listings if listing_matches_user(x, filters) and _keep_if_not_old(x, 7)
    ]
    return listings[:limit]


def _store_listings(listings: list[Listing]) -> None:
    for item in listings:
        db.upsert_catalog(
            {
                "id": item.id,
                "title": item.title,
                "price": item.price,
                "location": item.location,
                "area": item.area,
                "url": item.url,
                "source": item.source,
            }
        )


async def show_wizard_rooms(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    *,
    as_new_message: bool = False,
) -> None:
    filters = db.get_filters(user_id)
    text = wizard_rooms_text()
    markup = rooms_wizard_keyboard(filters)
    if as_new_message and update.message:
        await update.message.reply_text(text, reply_markup=markup)
    else:
        await _edit(update, text, markup)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await _safe_answer(query)

    user_id = update.effective_user.id
    data = query.data
    filters = db.get_filters(user_id)

    # --- Wizard ---
    if data == "wiz:start":
        db.set_awaiting(user_id, "wiz_budget")
        await _edit(update, wizard_budget_text(), budget_wizard_keyboard())
        return

    if data.startswith("wiz:budget:"):
        idx = int(data.split(":")[-1])
        _label, mn, mx = BUDGET_RANGES[idx]
        filters = db.update_filter(user_id, min_price=mn, max_price=mx)
        db.set_awaiting(user_id, None)
        await _edit(update, wizard_rooms_text(), rooms_wizard_keyboard(filters))
        return

    if data.startswith("wiz:room:"):
        n = int(data.split(":")[-1])
        rooms = list(filters.get("rooms") or [])
        if n in rooms:
            rooms.remove(n)
        else:
            rooms.append(n)
        filters = db.update_filter(user_id, rooms=sorted(rooms))
        await _edit(update, wizard_rooms_text(), rooms_wizard_keyboard(filters))
        return

    if data == "wiz:back:budget":
        db.set_awaiting(user_id, "wiz_budget")
        await _edit(update, wizard_budget_text(), budget_wizard_keyboard())
        return

    if data == "wiz:next:districts" or data == "wiz:back:rooms":
        # next districts OR returning to rooms handled below
        if data == "wiz:back:rooms":
            await _edit(update, wizard_rooms_text(), rooms_wizard_keyboard(filters))
            return
        await _edit(
            update, wizard_districts_text(), districts_wizard_keyboard(filters)
        )
        return

    if data.startswith("wiz:dist:"):
        token = data.split(":")[-1]
        if token == "all":
            filters = db.update_filter(user_id, districts=[])
        else:
            idx = int(token)
            name = DISTRICTS[idx]
            selected = list(filters.get("districts") or [])
            if name in selected:
                selected.remove(name)
            else:
                selected.append(name)
            filters = db.update_filter(user_id, districts=selected)
        await _edit(
            update, wizard_districts_text(), districts_wizard_keyboard(filters)
        )
        return

    if data == "wiz:done":
        filters = db.update_filter(user_id, setup_done=True)
        db.set_watching(user_id, True)
        await _edit(
            update,
            "✅ Пошук збережено. Все безкоштовно — розсилка вже працює.\n"
            "Стежу за OLX і Otodom і надсилатиму нові одразу.\n\n"
            f"{filters_summary(filters)}\n\n"
            "Зараз надішлю кілька актуальних оголошень…",
            main_menu_keyboard(),
        )
        try:
            listings = await _run_search(filters, limit=6)
        except Exception as exc:
            logger.exception("initial search failed")
            await context.bot.send_message(chat_id=user_id, text=f"Помилка: {exc}")
            return
        if not listings:
            await context.bot.send_message(
                chat_id=user_id,
                text="Поки нічого не знайшов — стежу далі і надішлю, щойно з’явиться.",
            )
            return
        _store_listings(listings)
        seen = db.load_seen()
        for item in listings:
            if not listing_matches_user(item, filters):
                continue
            seen.add(item.id)
            db.upsert_catalog(listing_record(item))
            await send_listing(context, user_id, item)
        db.save_seen(seen)
        return

    # --- Main menu ---
    if data == "menu:home":
        if not filters.get("setup_done"):
            await _edit(update, welcome_text(), start_keyboard())
            return
        text, markup = _home_markup(user_id, filters)
        await _edit(update, text, markup)
        return

    if data == "menu:pause":
        db.set_watching(user_id, False)
        text, markup = _home_markup(user_id, filters)
        await _edit(update, text, markup)
        return

    if data == "menu:resume":
        db.set_watching(user_id, True)
        text, markup = _home_markup(user_id, filters)
        await _edit(update, text, markup)
        return

    if data == "menu:filters":
        # нове повідомлення — надійніше, ніж edit після довгої розсилки
        await context.bot.send_message(
            chat_id=user_id,
            text=filters_menu_text(filters),
            reply_markup=filters_menu_keyboard(),
        )
        return

    if data == "menu:search":
        await _edit(
            update,
            f"{filters_summary(filters)}\n\n"
            "🔍 Шукаю на OLX і Otodom…",
            main_menu_keyboard(),
        )
        user_first_on()
        try:
            try:
                listings = await asyncio.wait_for(_run_search(filters, limit=8), timeout=90)
            except TimeoutError:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="⏱ Пошук занадто довгий. Спробуй ще раз через хвилину.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            except Exception as exc:
                logger.exception("search failed")
                await context.bot.send_message(chat_id=user_id, text=f"Помилка: {exc}")
                return
            if not listings:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="Немає свіжих оголошень за останні 7 днів під твої фільтри.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            _store_listings(listings)
            seen = db.load_seen()
            for item in listings:
                seen.add(item.id)
                db.upsert_catalog(listing_record(item))
                await send_listing(context, user_id, item)
            db.save_seen(seen)
        finally:
            user_first_off()
        return

    if data == "menu:week":
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "📅 Збираю всі оголошення за 7 днів з OLX і Otodom.\n"
                "Надішлю від найстарших до найновіших, з фото і датою як на сайті (час Польщі)."
            ),
        )
        user_first_on()
        try:
            try:
                week = await asyncio.wait_for(
                    run_browser(watch_week_listings, filters, 7),
                    timeout=240,
                )
            except TimeoutError:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="⏱ Збір зайняв занадто довго. Спробуй ще раз.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            except Exception as exc:
                logger.exception("week search failed")
                await context.bot.send_message(chat_id=user_id, text=f"Помилка: {exc}")
                return
            if not week:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="За 7 днів під ці фільтри нічого не знайшов.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            await context.bot.send_message(
                chat_id=user_id,
                text=f"Знайшов {len(week)}. Надсилаю від найстарших до найновіших.",
            )
            sent = 0
            for item in week:
                db.upsert_catalog(listing_record(item))
                try:
                    await send_listing(
                        context,
                        user_id,
                        item,
                        display_tz="Europe/Warsaw",
                        date_lang="pl",
                    )
                    sent += 1
                except Exception:
                    logger.exception("week send failed %s", item.id)
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"✅ Надіслав {sent} оголошень за 7 днів "
                    "(від найстарших до найновіших, дата — польський час).\n"
                    "Нові з OLX/Otodom приходитимуть самі."
                ),
                reply_markup=main_menu_keyboard(),
            )
        finally:
            user_first_off()
        return

    if data == "menu:catalog":
        rows = db.get_catalog(limit=12)
        if not rows:
            await _edit(
                update,
                "📂 Каталог порожній. Спочатку зроби пошук.",
                main_menu_keyboard(),
            )
            return
        await _edit(
            update,
            f"📂 Каталог (останні {len(rows)}):",
            main_menu_keyboard(),
        )
        for row in rows:
            text = (
                f"🏠 {row['title']}\n"
                f"💰 {row.get('price') or '—'}\n"
                f"📍 {row.get('location') or '—'}\n"
                f"📐 {row.get('area') or '—'}\n"
                f"🔗 {row['url']}"
            )
            await context.bot.send_message(chat_id=user_id, text=text)
        return

    # --- Settings submenus ---
    if data == "flt:districts":
        await _edit(
            update,
            wizard_districts_text(),
            districts_settings_keyboard(filters),
        )
        return

    if data == "flt:budget":
        await _edit(
            update,
            "💰 Бюджет = оренда + чинш + оплати на місяць.\n"
            "Обери діапазон або введи вручну (напр. 2000-3500):",
            budget_settings_keyboard(filters),
        )
        return

    if data == "flt:rooms":
        await _edit(
            update,
            "🚪 Обери кількість кімнат (можна кілька):",
            rooms_settings_keyboard(filters),
        )
        return

    if data == "flt:pets":
        await _edit(update, "🐶 Тварини:", pets_keyboard(filters))
        return

    if data == "flt:najem":
        await _edit(update, "📜 Najem okazjonalny:", najem_keyboard(filters))
        return

    if data == "flt:owner":
        await _edit(update, "👤 Тип власника:", owner_keyboard(filters))
        return

    if data == "flt:quiet":
        await _edit(
            update,
            "🔕 Вночі бот мовчить; зранку надсилає зібрані оголошення.",
            quiet_keyboard(filters),
        )
        return

    if data == "flt:tz":
        await _edit(
            update,
            "🕐 Час «Додано …» як на твоєму телефоні:",
            timezone_keyboard(filters),
        )
        return

    if data == "quiet:toggle":
        enabled = not bool(filters.get("quiet_hours"))
        filters = db.update_filter(
            user_id,
            quiet_hours=enabled,
            quiet_from="23:00",
            quiet_to="07:00",
        )
        await _edit(
            update,
            "🔕 Тихі години оновлено.",
            quiet_keyboard(filters),
        )
        return

    if data.startswith("tz:set:"):
        tz = data.removeprefix("tz:set:")
        filters = db.update_filter(user_id, display_tz=tz)
        await _edit(
            update,
            f"🕐 Часовий пояс: {tz}",
            timezone_keyboard(filters),
        )
        return

    if data.startswith("budget:set:"):
        idx = int(data.split(":")[-1])
        _label, mn, mx = BUDGET_RANGES[idx]
        filters = db.update_filter(user_id, min_price=mn, max_price=mx)
        await _edit(
            update,
            f"💰 Бюджет оновлено: {_label}",
            budget_settings_keyboard(filters),
        )
        return

    if data == "budget:manual":
        db.set_awaiting(user_id, "budget")
        await _edit(
            update,
            "✏️ Напиши діапазон у чат, наприклад 1800-3200\nАбо /cancel",
            budget_settings_keyboard(filters),
        )
        return

    if data.startswith("rooms:toggle:"):
        n = int(data.split(":")[-1])
        rooms = list(filters.get("rooms") or [])
        if n in rooms:
            rooms.remove(n)
        else:
            rooms.append(n)
        filters = db.update_filter(user_id, rooms=sorted(rooms))
        await _edit(
            update,
            "🚪 Обери кількість кімнат (можна кілька):",
            rooms_settings_keyboard(filters),
        )
        return

    if data == "rooms:clear":
        filters = db.update_filter(user_id, rooms=[])
        await _edit(
            update,
            "🚪 Будь-яка кількість кімнат.",
            rooms_settings_keyboard(filters),
        )
        return

    if data.startswith("set:dist:"):
        token = data.split(":")[-1]
        if token == "all":
            filters = db.update_filter(user_id, districts=[])
        else:
            idx = int(token)
            name = DISTRICTS[idx]
            selected = list(filters.get("districts") or [])
            if name in selected:
                selected.remove(name)
            else:
                selected.append(name)
            filters = db.update_filter(user_id, districts=selected)
        await _edit(
            update, wizard_districts_text(), districts_settings_keyboard(filters)
        )
        return

    if data.startswith("pets:set:"):
        value = data.split(":")[-1]
        filters = db.update_filter(user_id, pets=value)
        await _edit(update, "🐶 Оновлено.", pets_keyboard(filters))
        return

    if data.startswith("najem:set:"):
        value = data.split(":")[-1]
        filters = db.update_filter(user_id, najem_okazjonalny=value)
        await _edit(update, "📜 Оновлено.", najem_keyboard(filters))
        return

    if data.startswith("owner:set:"):
        value = data.split(":")[-1]
        filters = db.update_filter(user_id, owner_type=value)
        await _edit(update, "👤 Оновлено.", owner_keyboard(filters))
        return
