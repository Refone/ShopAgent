import asyncio
import selectors
from collections.abc import Coroutine


def run_async[ResultT](coro: Coroutine) -> ResultT:
    """
    等于 asyncio.run(), 为了解决 Windows 平台下的 ProactorEventLoop 事件循环问题。
    """

    return asyncio.run(
        coro,
        loop_factory=lambda: asyncio.SelectorEventLoop(
            selectors.SelectSelector(),
        ),
    )
