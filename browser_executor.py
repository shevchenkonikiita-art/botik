"""Усі Playwright-виклики — в одному потоці (без конфлікту з asyncio)."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, TypeVar

T = TypeVar("T")

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")
# Кнопка в меню важливіша за фоновий watch: watch обривається між сторінками.
_USER_FIRST = threading.Event()


def user_first_on() -> None:
    _USER_FIRST.set()


def user_first_off() -> None:
    _USER_FIRST.clear()


def user_waiting() -> bool:
    return _USER_FIRST.is_set()


async def run_browser(fn: Callable[..., T], *args, **kwargs) -> T:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, partial(fn, *args, **kwargs))
