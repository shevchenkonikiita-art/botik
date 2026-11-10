"""Тексти меню та підсумки фільтрів."""

from __future__ import annotations

from typing import Any

from config import CHECK_INTERVAL, CITY


def _pets_label(value: str) -> str:
    return {"all": "Всі", "yes": "Тільки з тваринами", "no": "Без тварин"}.get(
        value, value
    )


def _najem_label(value: str) -> str:
    return {"any": "Не важливо", "only": "Тільки okazjonalny"}.get(value, value)


def _owner_label(value: str) -> str:
    return {
        "all": "Всі",
        "private": "Тільки приватні",
        "agency": "Тільки агентства",
    }.get(value, value)


def _rooms_label(rooms: list[int] | None) -> str:
    if not rooms:
        return "будь-які"
    return ", ".join(str(r) if r < 4 else "4+" for r in sorted(rooms))


def _budget_label(filters: dict[str, Any]) -> str:
    mn = int(filters.get("min_price") or 0)
    mx = int(filters.get("max_price") or 0)
    suffix = " (з чиншем і оплатами)"
    if mn <= 0 and mx >= 90000:
        return "не важливо"
    if mn <= 0:
        return f"до {mx} zł{suffix}"
    if mx >= 90000:
        return f"від {mn} zł{suffix}"
    return f"{mn}–{mx} zł{suffix}"


def filters_summary(filters: dict[str, Any]) -> str:
    districts = filters.get("districts") or []
    districts_text = ", ".join(districts) if districts else "вся Познань"
    return (
        f"Активні фільтри ({CITY}):\n"
        f"• 💰 Бюджет: {_budget_label(filters)}\n"
        f"• 🚪 Кімнати: {_rooms_label(filters.get('rooms'))}\n"
        f"• 📍 Райони: {districts_text}\n"
        f"• 🐶 Тварини: {_pets_label(filters.get('pets', 'all'))}\n"
        f"• 📜 Najem okazjonalny: {_najem_label(filters.get('najem_okazjonalny', 'any'))}\n"
        f"• 👤 Власник: {_owner_label(filters.get('owner_type', 'all'))}"
    )


def welcome_text() -> str:
    return (
        f"FindHome — оренда в {CITY} (як @thewarsawrentbot, але без підписки на канал).\n\n"
        "• OLX + Otodom, сортування «найновіші»\n"
        "• Фото, ціна з чиншем, вулиця на карті\n"
        "• Нові оголошення — одразу під ваші фільтри\n"
        "• Тихі години (вночі не будить, зранку пришле пакет)\n\n"
        "Натисни «Створити пошук»."
    )


def _quiet_label(filters: dict[str, Any]) -> str:
    if not filters.get("quiet_hours"):
        return "вимкнені"
    return f"{filters.get('quiet_from', '23:00')}–{filters.get('quiet_to', '07:00')}"


def home_text(filters: dict[str, Any], *, watching: bool = True) -> str:
    watch_line = (
        f"✅ Авторозсилка увімкнена (OLX+Otodom кожні {CHECK_INTERVAL} с)"
        if watching
        else "⏸ Авторозсилка на паузі"
    )
    return (
        f"FindHome · {CITY}\n"
        "100% безкоштовно · без каналів і преміуму\n"
        f"{watch_line}\n"
        f"🔕 Тихі години: {_quiet_label(filters)}\n\n"
        f"{filters_summary(filters)}\n\n"
        "Обери дію:"
    )


def filters_menu_text(filters: dict[str, Any]) -> str:
    return (
        "🛠 Налаштування фільтрів\n\n"
        f"{filters_summary(filters)}\n\n"
        "Натисни категорію, щоб змінити:"
    )


def wizard_budget_text() -> str:
    return (
        "Крок 1 з 3 · 💰 Бюджет\n\n"
        "Скільки готові платити НА МІСЯЦЬ ЗАВРАЗОМ — "
        "оренда + чинш + media/оплати?\n\n"
        "Приклад: якщо пишете 3500 — покажу лише квартири, "
        "де повна сума ≈ до 3500 zł.\n"
        "Оберіть діапазон або напишіть свій: 2000-3500."
    )


def wizard_rooms_text() -> str:
    return (
        "Крок 2 з 3 · 🚪 Кімнати\n\n"
        "Відмітьте, скільки кімнат підходить — можна кілька.\n"
        "Нічого не відмічено — значить, підійде будь-яка."
    )


def wizard_districts_text() -> str:
    return (
        f"Крок 3 з 3 · 📍 Райони\n\n"
        f"Відмітьте райони. Нічого не відмічено — шукаю по всій {CITY}.\n"
        "Назви залишені польською: так вони написані на карті та в самому оголошенні."
    )
