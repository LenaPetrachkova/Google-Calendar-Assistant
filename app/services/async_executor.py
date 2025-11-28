from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar, ParamSpec

P = ParamSpec("P")
R = TypeVar("R")

_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="calendar_async_",
)


def run_sync(func: Callable[P, R]) -> Callable[P, asyncio.Coroutine[None, None, R]]:
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, lambda: func(*args, **kwargs))
    
    return wrapper


async def run_in_executor(
    func: Callable[P, R],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: func(*args, **kwargs))


def shutdown_executor() -> None:
    _executor.shutdown(wait=True)

