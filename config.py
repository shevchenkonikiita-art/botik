"""Налаштування бота для пошуку квартир у Познані (повністю безкоштовно)."""

import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

CITY = "Poznań"
CITY_SLUG = "poznan"

DISTRICTS = [
    "Stare Miasto",
    "Jeżyce",
    "Grunwald",
    "Wilda",
    "Rataje",
    "Winogrady",
    "Łazarz",
    "Piątkowo",
    "Nowe Miasto",
    "Naramowice",
    "Górczyn",
    "Łacina",
    "Ogrody",
    "Dębiec",
    "Winiary",
    "Sołacz",
    "Chartowo",
    "Podolany",
    "Junikowo",
    "Strzeszyn",
]

BUDGET_RANGES = [
    ("до 2500 zł", 0, 2500),
    ("2500–3500 zł", 2500, 3500),
    ("3500–5000 zł", 3500, 5000),
    ("від 5000 zł", 5000, 99999),
    ("Не важливо", 0, 99999),
]

ROOM_OPTIONS = [
    (1, "1 кімната"),
    (2, "2 кімнати"),
    (3, "3 кімнати"),
    (4, "4+ кімнати"),
]

# Час на картці — у поясі користувача (IANA). Приклад: Europe/Kyiv, America/Chicago
DISPLAY_TZ = os.getenv("DISPLAY_TZ", "Europe/Kyiv")

DEFAULT_FILTERS = {
    "min_price": 0,
    "max_price": 99999,
    "rooms": [],
    "min_area": 15,
    "max_area": 120,
    "districts": [],
    "pets": "all",
    "najem_okazjonalny": "any",
    "owner_type": "all",
    "setup_done": False,
    "display_tz": DISPLAY_TZ,
    "quiet_hours": False,
    "quiet_from": "23:00",
    "quiet_to": "07:00",
}

# Джерела: усе безкоштовно
SOURCES = ("olx", "otodom", "facebook")

# Інтервал фонової перевірки OLX/Otodom (секунди).
# 1 с неможливо — один прохід браузером ~20–40 с; мінімум 10 с (через .env).
# Приклад у .env: CHECK_INTERVAL=15
# Warsaw Rent ≈ 1 хв; у .env можна 10–15 для агресивнішого режиму.
CHECK_INTERVAL = max(10, int(os.getenv("CHECK_INTERVAL", "20")))

# Автоповідомлення: ігноруємо лише явно старі (дата відома і > N днів).
NEW_LISTING_MAX_AGE_DAYS = max(1, int(os.getenv("NEW_LISTING_MAX_AGE_DAYS", "14")))

# Скільки карток читаємо з кожної сторінки LATEST (watch не обрізає раніше за ліміт).
WATCH_MAX_CARD_SCAN = max(24, int(os.getenv("WATCH_MAX_CARD_SCAN", "56")))
# Сторінок «найновіші» на джерело (1 зазвичай достатньо при повному скані сторінки).
WATCH_PAGES = max(1, min(3, int(os.getenv("WATCH_PAGES", "1"))))

# Зворотна сумісність (limit для меню; watch ігнорує ранній stop у _filter_list).
WATCH_FEED_PER_SOURCE = max(12, int(os.getenv("WATCH_FEED_PER_SOURCE", "48")))
