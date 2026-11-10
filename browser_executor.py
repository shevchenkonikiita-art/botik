"""Усі Playwright-виклики — в одному потоці (без конфлікту з asyncio)."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, TypeVar

T = TypeVar("T")

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")


async def run_browser(fn: Callable[..., T], *args, **kwargs) -> T:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, partial(fn, *args, **kwargs))
