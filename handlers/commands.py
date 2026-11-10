"""Команди та текстові відповіді (ручне введення бюджету)."""

from __future__ import annotations

import re

from telegram import Update
from telegram.ext import ContextTypes

import db
from handlers.keyboards import main_menu_keyboard, start_keyboard
from handlers.texts import home_text, welcome_text

BUDGET_RANGE_RE = re.compile(r"^\s*(\d+)\s*[-–]\s*(\d+)\s*$")
BUDGET_SINGLE_RE = re.compile(r"^\s*(\d+)\s*$")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    filters = db.get_filters(user_id)
    db.save_filters(user_id, filters)
    db.set_awaiting(user_id, None)

    if filters.get("setup_done"):
        db.set_watching(user_id, True)
        watching = db.is_watching(user_id)
        await update.message.reply_text(
            home_text(filters, watching=watching),
            reply_markup=main_menu_keyboard(watching=watching),
        )
        return

    await update.message.reply_text(
        welcome_text(),
        reply_markup=start_keyboard(),
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    filters = db.get_filters(user_id)
    if not filters.get("setup_done"):
        await update.message.reply_text(
            welcome_text(),
            reply_markup=start_keyboard(),
        )
        return
    db.set_watching(user_id, True)
    watching = db.is_watching(user_id)
    await update.message.reply_text(
        home_text(filters, watching=watching),
        reply_markup=main_menu_keyboard(watching=watching),
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db.set_awaiting(user_id, None)
    filters = db.get_filters(user_id)
    await update.message.reply_text(
        "Скасовано.",
        reply_markup=(
            main_menu_keyboard() if filters.get("setup_done") else start_keyboard()
        ),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    user_id = update.effective_user.id
    awaiting = db.get_awaiting(user_id)
    if awaiting not in {"budget", "wiz_budget"}:
        return

    raw = update.message.text.strip().replace(" ", "")
    match = BUDGET_RANGE_RE.match(raw)
    if match:
        mn, mx = int(match.group(1)), int(match.group(2))
    else:
        single = BUDGET_SINGLE_RE.match(raw)
        if not single:
            await update.message.reply_text(
                "Напишіть діапазон, наприклад 2000-3500, або одну суму. Або /cancel"
            )
            return
        mn, mx = 0, int(single.group(1))

    if mn > mx or mx < 500 or mx > 30000:
        await update.message.reply_text("Перевір суми (500–30000 zł).")
        return

    db.update_filter(user_id, min_price=mn, max_price=mx)
    db.set_awaiting(user_id, None)

    if awaiting == "wiz_budget":
        from handlers.callbacks import show_wizard_rooms

        await show_wizard_rooms(update, context, user_id, as_new_message=True)
        return

    await update.message.reply_text(
        f"💰 Бюджет: {mn}–{mx} zł (з чиншем і оплатами)",
        reply_markup=main_menu_keyboard(),
    )
