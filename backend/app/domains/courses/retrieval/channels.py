"""并行召回共享计时和故障回执；取消必须向上游传播。"""

import asyncio
import time


async def run_channels(work, timings, errors):
    async def measured(name, call):
        started = time.perf_counter()
        try:
            return await call()
        finally:
            timings.setdefault(name + "_ms", round((time.perf_counter() - started) * 1000, 2))
    results = await asyncio.gather(*(measured(name, call) for name, call in work.items()), return_exceptions=True)
    channels = {}
    for name, result in zip(work, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            errors.append(f"{name}:{type(result).__name__}")
        elif isinstance(result, BaseException):
            raise result
        else:
            channels[name] = result["hits"]["hits"]
    return channels
