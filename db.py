"""SQLite-сховище: фільтри користувачів, підписки, каталог оголошень."""

from __future__ import annotations

import json
import sqlite3
import os
from pathlib import Path
from typing import Any

from config import DEFAULT_FILTERS

_DATA_DIR = Path(
    os.getenv("FINDHOME_DATA_DIR", str(Path(__file__).resolve().parent))
).expanduser()
DB_PATH = _DATA_DIR / "findhome.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                filters_json TEXT NOT NULL,
                watching INTEGER NOT NULL DEFAULT 0,
                awaiting TEXT
            );

            CREATE TABLE IF NOT EXISTS catalog (
                listing_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                price TEXT,
                location TEXT,
                area TEXT,
                url TEXT NOT NULL,
                source TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS seen (
                listing_id TEXT PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS notify_queue (
                user_id INTEGER NOT NULL,
                listing_id TEXT NOT NULL,
                snapshot TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, listing_id)
            );
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(catalog)")}
        if "payload" not in cols:
            conn.execute("ALTER TABLE catalog ADD COLUMN payload TEXT")
        if "posted_at" not in cols:
            conn.execute("ALTER TABLE catalog ADD COLUMN posted_at TEXT")


def _default_filters() -> dict[str, Any]:
    return {
        **DEFAULT_FILTERS,
        "pets": "all",  # all | yes | no
        "najem_okazjonalny": "any",  # any | only
        "owner_type": "all",  # all | private | agency
    }


def get_filters(user_id: int) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT filters_json FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return _default_filters()
    try:
        saved = json.loads(row["filters_json"])
    except json.JSONDecodeError:
        return _default_filters()
    return {**_default_filters(), **saved}


def save_filters(user_id: int, filters: dict[str, Any]) -> None:
    payload = json.dumps(filters, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, filters_json, watching)
            VALUES (?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET filters_json = excluded.filters_json
            """,
            (user_id, payload),
        )


def update_filter(user_id: int, **kwargs: Any) -> dict[str, Any]:
    filters = get_filters(user_id)
    filters.update(kwargs)
    save_filters(user_id, filters)
    return filters


def is_watching(user_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT watching FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return bool(row and row["watching"])


def set_watching(user_id: int, enabled: bool) -> None:
    filters = get_filters(user_id)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, filters_json, watching)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET watching = excluded.watching
            """,
            (user_id, json.dumps(filters, ensure_ascii=False), int(enabled)),
        )


def list_watchers() -> list[int]:
    """Усі користувачі з увімкненою розсилкою (безкоштовно для всіх)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE watching = 1"
        ).fetchall()
    return [int(r["user_id"]) for r in rows]


def list_active_users() -> list[int]:
    """Усі, хто завершив налаштування — отримують оголошення безкоштовно."""
    with _connect() as conn:
        rows = conn.execute("SELECT user_id, filters_json, watching FROM users").fetchall()
    result: list[int] = []
    for row in rows:
        try:
            data = json.loads(row["filters_json"] or "{}")
        except json.JSONDecodeError:
            data = {}
        if data.get("setup_done") and row["watching"]:
            result.append(int(row["user_id"]))
    return result


def set_awaiting(user_id: int, value: str | None) -> None:
    filters = get_filters(user_id)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, filters_json, watching, awaiting)
            VALUES (?, ?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET awaiting = excluded.awaiting
            """,
            (user_id, json.dumps(filters, ensure_ascii=False), value),
        )


def get_awaiting(user_id: int) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT awaiting FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return None
    return row["awaiting"]


def upsert_catalog(listing: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO catalog (
                listing_id, title, price, location, area, url, source, payload, posted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_id) DO UPDATE SET
                title = excluded.title,
                price = excluded.price,
                location = excluded.location,
                area = excluded.area,
                url = excluded.url,
                source = excluded.source,
                payload = COALESCE(excluded.payload, catalog.payload),
                posted_at = COALESCE(excluded.posted_at, catalog.posted_at)
            """,
            (
                listing["id"],
                listing["title"],
                listing.get("price"),
                listing.get("location"),
                listing.get("area"),
                listing["url"],
                listing.get("source"),
                listing.get("payload"),
                listing.get("posted_at"),
            ),
        )


def get_catalog(limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT listing_id, title, price, location, area, url, source, created_at
            FROM catalog
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def load_seen() -> set[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT listing_id FROM seen").fetchall()
    return {r["listing_id"] for r in rows}


def save_seen(seen: set[str]) -> None:
    items = list(seen)[-2000:]
    with _connect() as conn:
        conn.execute("DELETE FROM seen")
        conn.executemany(
            "INSERT OR IGNORE INTO seen (listing_id) VALUES (?)",
            [(x,) for x in items],
        )


def mark_seen_many(ids: list[str]) -> None:
    if not ids:
        return
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO seen (listing_id) VALUES (?)",
            [(x,) for x in ids],
        )


def queue_listing_snapshot(user_id: int, snapshot: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO notify_queue (user_id, listing_id, snapshot)
            VALUES (?, ?, ?)
            """,
            (user_id, snapshot["id"], json.dumps(snapshot, ensure_ascii=False)),
        )


def pop_notify_queue(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT listing_id, snapshot FROM notify_queue
            WHERE user_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        if not rows:
            return []
        ids = [r["listing_id"] for r in rows]
        conn.executemany(
            "DELETE FROM notify_queue WHERE user_id = ? AND listing_id = ?",
            [(user_id, lid) for lid in ids],
        )
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            result.append(json.loads(row["snapshot"]))
        except json.JSONDecodeError:
            continue
    return result


def notify_queue_count(user_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM notify_queue WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row["c"]) if row else 0
