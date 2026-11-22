"""有界临时 SSE 通道；正式业务结果由领域服务持久化。"""

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from uuid import uuid4

from fastapi import Request
from fastapi.responses import StreamingResponse

from app.api.dependencies import services
from app.shared.execution import LearningConflict

QUEUE_CAPACITY = 256
DIAGNOSTICS = frozenset({"context_budget", "context_purpose_selected", "memory_context_loaded", "model_request_completed", "agent_failed"})
diagnostics = logging.getLogger("tutor.diagnostics")
if (diagnostic_path := os.getenv("TUTOR_DIAGNOSTIC_LOG")) and not diagnostics.handlers:
    # 验收可显式开启有上限的指标日志；默认不保存临时事件，不记录学生正文。
    handler = RotatingFileHandler(diagnostic_path, maxBytes=8 * 1024 * 1024, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    diagnostics.addHandler(handler)
    diagnostics.setLevel(logging.INFO)
    diagnostics.propagate = False


async def execute_operation(
    request: Request, session_id: str, operation, *, stream: bool
):
    """为学习、出题和作答操作提供统一的后台执行与 SSE 事件通道。

    同一操作共享 turn_id；流式模式传递正文和运行进度，非流式直接返回。
    缓冲溢出或消费端断开只结束临时流，后台任务继续保存正式结果。
    """
    services(request)["sessions"].get(session_id)
    turn_id = str(uuid4())
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_CAPACITY)
    listening = stream
    sequence = 0

    def close_channel():
        nonlocal listening
        listening = False
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(None)

    async def emit(event: str, data: dict) -> None:
        """绝不等待慢消费端；满队列结束临时流，由前端快照恢复。"""
        nonlocal sequence
        sequence += 1
        if event in DIAGNOSTICS:
            diagnostics.info(json.dumps({"id": f"{turn_id}:{sequence}", "session_id": session_id,
                "turn_id": turn_id, "seq": sequence, "event": event, "data": data}, ensure_ascii=False))
        if not listening:
            return
        try:
            queue.put_nowait({"id": f"{turn_id}:{sequence}", "event": event, "data": data})
        except asyncio.QueueFull:
            close_channel()

    async def perform():
        """执行业务操作并发出完成或错误事件，结束标记始终写入队列以关闭事件流。"""
        try:
            result = await operation(turn_id, emit)
            from app.api.operation_memory import capture_operation_memory

            capture_operation_memory(request, session_id, result)
            await emit("done", {"turn_id": turn_id, "status": "completed"})
            return result
        except Exception as exc:
            await emit(
                "error",
                {
                    "turn_id": turn_id,
                    "message": str(exc)[:300],
                    "error": type(exc).__name__,
                    "status_code": 409 if isinstance(exc, LearningConflict) else 400 if isinstance(exc, ValueError) else 500,
                },
            )
            raise
        finally:
            if listening:
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    close_channel()

    if not stream:
        return await perform()
    task = asyncio.create_task(perform())
    request.app.state.tasks.add(task)

    def completed(finished):
        """回收应用持有的后台任务引用，并读取已通过事件记录的异常。"""
        request.app.state.tasks.discard(finished)
        # 显式读取异常；正式结果和 AgentRun 是恢复来源。
        if not finished.cancelled():
            finished.exception()

    task.add_done_callback(completed)

    async def event_stream():
        """消费本轮事件队列并编码为 SSE；空闲 12 秒发送心跳维持连接。"""
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=12)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event is None:
                    break
                yield f"id: {event['id']}\nevent: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
        finally:
            close_channel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
