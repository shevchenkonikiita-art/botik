"""FindHome Bot — безкоштовний моніторинг OLX / Otodom (як Warsaw Rent, без преміуму)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from browser_executor import run_browser, user_waiting

from telegram import BotCommand, MenuButtonCommands, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from config import (
    BOT_TOKEN,
    CHECK_INTERVAL,
    NEW_LISTING_MAX_AGE_DAYS,
    WATCH_FEED_PER_SOURCE,
)
from filters_util import merge_filters_for_watch, watch_might_match_user
from handlers.callbacks import on_callback
from handlers.commands import cancel_cmd, menu_cmd, on_text, start_cmd
from handlers.send import send_listing
from notify_util import in_quiet_hours
from scraper import (
    is_fresh_for_watch,
    listing_from_snapshot,
    listing_matches_user,
    listing_posted_at,
    listing_record,
    listing_snapshot,
)
from watch_browser import watch_enrich_many, watch_fetch_listings

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def _deliver_listing(context: ContextTypes.DEFAULT_TYPE, user_id: int, item) -> None:
    user_f = db.get_filters(user_id)
    if in_quiet_hours(user_f):
        db.queue_listing_snapshot(user_id, listing_snapshot(item))
        db.upsert_catalog(listing_record(item))
        return
    try:
        await send_listing(context, user_id, item)
    except Exception:
        logger.exception("notify failed for %s", user_id)
    db.upsert_catalog(listing_record(item))


async def _flush_queued(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    user_f = db.get_filters(user_id)
    if in_quiet_hours(user_f):
        return
    queued = db.pop_notify_queue(user_id)
    if not queued:
        return
    for snap in queued:
        try:
            await send_listing(context, user_id, listing_from_snapshot(snap))
        except Exception:
            logger.exception("queue flush failed %s", user_id)
    if queued:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🌅 Надіслав {len(queued)} оголошень, зібраних під час тихих годин.",
        )


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Запустити бота / головне меню"),
            BotCommand("menu", "Відкрити меню"),
            BotCommand("cancel", "Скасувати введення"),
        ]
    )
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    logger.info("Menu button + commands configured")


async def watch_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if user_waiting():
        return
    users = db.list_active_users()
    if not users:
        return

    for user_id in users:
        await _flush_queued(context, user_id)

    user_filters = {uid: db.get_filters(uid) for uid in users}
    scan_filters = merge_filters_for_watch(list(user_filters.values()))

    base: list = []
    for attempt in range(2):
        try:
            base = await run_browser(
                watch_fetch_listings, scan_filters, WATCH_FEED_PER_SOURCE
            )
            break
        except Exception:
            logger.exception("watch search failed (attempt %s)", attempt + 1)
    if not base:
        return

    seen = db.load_seen()
    unseen = [x for x in base if x.id not in seen]
    if not unseen:
        return

    logger.info(
        "Watch feed: %s cards (full LATEST scan), unseen: %s",
        len(base),
        len(unseen),
    )

    ready = [uid for uid in users if user_filters[uid].get("setup_done")]
    matched = [
        x
        for x in unseen
        if any(watch_might_match_user(x, user_filters[uid]) for uid in ready)
    ]
    stale_ids = [x.id for x in unseen if x not in matched]
    fresh = [
        x
        for x in matched
        if is_fresh_for_watch(x, NEW_LISTING_MAX_AGE_DAYS)
    ]
    old_on_feed = [x.id for x in matched if x not in fresh]
    db.mark_seen_many(stale_ids + old_on_feed)

    brand_new = fresh[:8]
    leftover = fresh[8:]
    if leftover:
        logger.info("watch leftover %s for next cycle", len(leftover))
    if not brand_new:
        return

    try:
        brand_new = await run_browser(watch_enrich_many, brand_new)
    except Exception:
        logger.exception("watch enrich failed; sending card data")

    brand_new.sort(
        key=lambda x: listing_posted_at(x) or datetime.now(timezone.utc),
        reverse=True,
    )
    db.mark_seen_many([x.id for x in brand_new])

    logger.info(
        "Notify: %s new today/latest (enrich czynsz+photo)",
        len(brand_new),
    )
    for user_id in ready:
        user_f = user_filters[user_id]
        for item in brand_new:
            if not listing_matches_user(item, user_f):
                continue
            await _deliver_listing(context, user_id, item)


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "your_telegram_bot_token_here":
        raise SystemExit("Вкажи BOT_TOKEN у файлі .env")

    db.init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if app.job_queue is not None:
        app.job_queue.run_repeating(
            watch_job,
            interval=CHECK_INTERVAL,
            first=10,
            job_kwargs={"max_instances": 1},
        )
    else:
        logger.warning(
            'JobQueue недоступний. pip install "python-telegram-bot[job-queue]"'
        )

    logger.info(
        "FindHome Bot запущено (free · OLX/Otodom · кожні %ss · нові ≤%s дні)",
        CHECK_INTERVAL,
        NEW_LISTING_MAX_AGE_DAYS,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
