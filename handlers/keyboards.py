"""Inline Keyboard builders (повністю безкоштовний бот)."""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import BUDGET_RANGES, DISTRICTS, ROOM_OPTIONS


def box(selected: bool, label: str) -> str:
    return f"{'✅' if selected else '☐'} {label}"


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔍 Створити пошук", callback_data="wiz:start")]]
    )


def budget_wizard_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, (label, _mn, _mx) in enumerate(BUDGET_RANGES):
        if label == "Не важливо":
            if row:
                rows.append(row)
            rows.append(
                [InlineKeyboardButton(label, callback_data=f"wiz:budget:{i}")]
            )
            row = []
            continue
        row.append(InlineKeyboardButton(label, callback_data=f"wiz:budget:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def rooms_wizard_keyboard(filters: dict[str, Any]) -> InlineKeyboardMarkup:
    selected = set(filters.get("rooms") or [])
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for n, label in ROOM_OPTIONS:
        row.append(
            InlineKeyboardButton(
                box(n in selected, label),
                callback_data=f"wiz:room:{n}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton("← Назад", callback_data="wiz:back:budget"),
            InlineKeyboardButton("Далі →", callback_data="wiz:next:districts"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def districts_wizard_keyboard(filters: dict[str, Any]) -> InlineKeyboardMarkup:
    selected = set(filters.get("districts") or [])
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, name in enumerate(DISTRICTS):
        row.append(
            InlineKeyboardButton(
                box(name in selected, name),
                callback_data=f"wiz:dist:{i}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton("🧹 Вся Познань", callback_data="wiz:dist:all")]
    )
    rows.append(
        [
            InlineKeyboardButton("← Назад", callback_data="wiz:back:rooms"),
            InlineKeyboardButton("✅ Готово", callback_data="wiz:done"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def main_menu_keyboard(watching: bool = True) -> InlineKeyboardMarkup:
    pause_btn = (
        InlineKeyboardButton("⏸ Пауза розсилки", callback_data="menu:pause")
        if watching
        else InlineKeyboardButton("▶️ Увімкнути розсилку", callback_data="menu:resume")
    )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 Показати оголошення зараз", callback_data="menu:search")],
            [InlineKeyboardButton("📅 За 7 днів", callback_data="menu:week")],
            [InlineKeyboardButton("🛠 Змінити фільтри", callback_data="menu:filters")],
            [pause_btn],
            [InlineKeyboardButton("🔁 Новий пошук", callback_data="wiz:start")],
        ]
    )


def filters_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📍 Райони", callback_data="flt:districts")],
            [InlineKeyboardButton("💰 Бюджет", callback_data="flt:budget")],
            [InlineKeyboardButton("🚪 Кімнати", callback_data="flt:rooms")],
            [InlineKeyboardButton("🐶 Тварини", callback_data="flt:pets")],
            [
                InlineKeyboardButton(
                    "📜 Najem okazjonalny", callback_data="flt:najem"
                )
            ],
            [InlineKeyboardButton("👤 Тип власника", callback_data="flt:owner")],
            [InlineKeyboardButton("🔕 Тихі години", callback_data="flt:quiet")],
            [InlineKeyboardButton("🕐 Часовий пояс", callback_data="flt:tz")],
            [InlineKeyboardButton("⬅️ Головне меню", callback_data="menu:home")],
        ]
    )


def quiet_keyboard(filters: dict[str, Any]) -> InlineKeyboardMarkup:
    on = bool(filters.get("quiet_hours"))
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Увімкнено (23:00–07:00)" if on else "Увімкнути 23:00–07:00",
                    callback_data="quiet:toggle",
                )
            ],
            [InlineKeyboardButton("⬅️ Назад до фільтрів", callback_data="menu:filters")],
        ]
    )


def timezone_keyboard(filters: dict[str, Any]) -> InlineKeyboardMarkup:
    current = filters.get("display_tz", "Europe/Kyiv")
    options = [
        ("🇺🇦 Київ", "Europe/Kyiv"),
        ("🇵🇱 Варшава", "Europe/Warsaw"),
        ("🇺🇸 Chicago (UTC-6)", "America/Chicago"),
        ("🇺🇸 New York", "America/New_York"),
    ]
    rows = []
    for label, tz in options:
        mark = "✅ " if tz == current else ""
        rows.append(
            [InlineKeyboardButton(f"{mark}{label}", callback_data=f"tz:set:{tz}")]
        )
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:filters")])
    return InlineKeyboardMarkup(rows)


def budget_settings_keyboard(filters: dict[str, Any]) -> InlineKeyboardMarkup:
    mn = int(filters.get("min_price") or 0)
    mx = int(filters.get("max_price") or 0)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, (label, bmin, bmax) in enumerate(BUDGET_RANGES):
        selected = mn == bmin and mx == bmax
        btn = InlineKeyboardButton(
            box(selected, label) if label != "Не важливо" else label,
            callback_data=f"budget:set:{i}",
        )
        if label == "Не важливо":
            if row:
                rows.append(row)
                row = []
            rows.append([btn])
            continue
        row.append(btn)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton("✏️ Ввести вручну (мін-макс)", callback_data="budget:manual")]
    )
    rows.append(
        [InlineKeyboardButton("⬅️ Назад до фільтрів", callback_data="menu:filters")]
    )
    return InlineKeyboardMarkup(rows)


def rooms_settings_keyboard(filters: dict[str, Any]) -> InlineKeyboardMarkup:
    selected = set(filters.get("rooms") or [])
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for n, label in ROOM_OPTIONS:
        row.append(
            InlineKeyboardButton(
                box(n in selected, label),
                callback_data=f"rooms:toggle:{n}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton("Будь-яка кількість", callback_data="rooms:clear")]
    )
    rows.append(
        [InlineKeyboardButton("⬅️ Назад до фільтрів", callback_data="menu:filters")]
    )
    return InlineKeyboardMarkup(rows)


def districts_settings_keyboard(filters: dict[str, Any]) -> InlineKeyboardMarkup:
    selected = set(filters.get("districts") or [])
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, name in enumerate(DISTRICTS):
        row.append(
            InlineKeyboardButton(
                box(name in selected, name),
                callback_data=f"set:dist:{i}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton("🧹 Вся Познань", callback_data="set:dist:all")]
    )
    rows.append(
        [InlineKeyboardButton("⬅️ Назад до фільтрів", callback_data="menu:filters")]
    )
    return InlineKeyboardMarkup(rows)


def pets_keyboard(filters: dict[str, Any]) -> InlineKeyboardMarkup:
    current = filters.get("pets", "all")
    options = [
        ("all", "Всі"),
        ("yes", "Тільки з тваринами (Tak)"),
        ("no", "Без тварин"),
    ]
    rows = [
        [
            InlineKeyboardButton(
                box(current == key, label),
                callback_data=f"pets:set:{key}",
            )
        ]
        for key, label in options
    ]
    rows.append(
        [InlineKeyboardButton("⬅️ Назад до фільтрів", callback_data="menu:filters")]
    )
    return InlineKeyboardMarkup(rows)


def najem_keyboard(filters: dict[str, Any]) -> InlineKeyboardMarkup:
    current = filters.get("najem_okazjonalny", "any")
    options = [
        ("any", "Не важливо"),
        ("only", "Тільки з okazjonalnym"),
    ]
    rows = [
        [
            InlineKeyboardButton(
                box(current == key, label),
                callback_data=f"najem:set:{key}",
            )
        ]
        for key, label in options
    ]
    rows.append(
        [InlineKeyboardButton("⬅️ Назад до фільтрів", callback_data="menu:filters")]
    )
    return InlineKeyboardMarkup(rows)


def owner_keyboard(filters: dict[str, Any]) -> InlineKeyboardMarkup:
    current = filters.get("owner_type", "all")
    options = [
        ("all", "Всі"),
        ("private", "Тільки приватні"),
        ("agency", "Тільки агентства"),
    ]
    rows = [
        [
            InlineKeyboardButton(
                box(current == key, label),
                callback_data=f"owner:set:{key}",
            )
        ]
        for key, label in options
    ]
    rows.append(
        [InlineKeyboardButton("⬅️ Назад до фільтрів", callback_data="menu:filters")]
    )
    return InlineKeyboardMarkup(rows)


def listing_keyboard(url: str, maps_url: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔗 Відкрити оголошення", url=url)],
    ]
    if maps_url:
        rows.append([InlineKeyboardButton("🗺 Відкрити вулицю на карті", url=maps_url)])
    rows.append(
        [InlineKeyboardButton("🛠 Змінити фільтри", callback_data="menu:filters")]
    )
    return InlineKeyboardMarkup(rows)
