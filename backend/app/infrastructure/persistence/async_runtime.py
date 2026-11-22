"""Windows 的 PostgreSQL 异步连接使用 Selector 循环，避免修改全局策略。"""

import asyncio
import sys


def selector_loop():
    """供 Uvicorn 的自定义 loop_factory 和隔离验收进程调用。"""
    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop
