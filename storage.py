"""Сумісність: старі імпорти seen-оголошень ведуть у SQLite (db.py)."""

from db import load_seen, mark_seen_many, save_seen

__all__ = ["load_seen", "save_seen", "mark_seen_many"]
