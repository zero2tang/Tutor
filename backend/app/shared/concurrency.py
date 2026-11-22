"""在线程内执行同步只读工具，避免数据库和文件读取阻塞事件循环。"""

import asyncio


async def run_blocking(function, *args):
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Python 不能强制终止线程，等待只读查询关闭自己的连接后再释放执行槽。
        try:
            await worker
        except Exception:
            pass
        raise
