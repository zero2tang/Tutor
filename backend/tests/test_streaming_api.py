"""验证即时 SSE、有界缓冲与断线继续执行；临时事件不写数据库。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import Request
from fastapi.responses import StreamingResponse

from app.api.streaming import execute_operation


@pytest.fixture
def isolated_stream_request(monkeypatch):
    persisted = []

    def fake_database():
        raise AssertionError("临时事件不应访问数据库")

    monkeypatch.setattr("app.api.streaming.db_session", fake_database, raising=False)
    sessions = SimpleNamespace(get=Mock(return_value={"id": "session-test"}))
    app = SimpleNamespace(
        state=SimpleNamespace(services={"sessions": sessions}, tasks=set())
    )
    request = Request({"type": "http", "app": app})
    return request, persisted, sessions


def decode_frame(frame):
    if isinstance(frame, bytes):
        frame = frame.decode("utf-8")
    lines = frame.splitlines()
    return {
        "id": next(line[4:] for line in lines if line.startswith("id: ")),
        "event": next(line[7:] for line in lines if line.startswith("event: ")),
        "data": json.loads(
            "\n".join(line[6:] for line in lines if line.startswith("data: "))
        ),
    }


@pytest.mark.asyncio
async def test_sse_delivers_text_before_operation_completes(isolated_stream_request):
    request, persisted, sessions = isolated_stream_request
    release_tail = asyncio.Event()
    operation_completed = asyncio.Event()
    final_message = {
        "id": "message-test",
        "role": "assistant",
        "content": "先讲原理，再给例子。",
    }

    async def delayed_operation(turn_id, emit):
        await emit("text_delta", {"stream_id": "preview-test", "delta": "先讲原理，"})
        # 门闩比固定延时更严格：首帧必须在业务操作获准生成尾部之前可读。
        await release_tail.wait()
        await emit("text_reset", {"stream_id": "preview-test"})
        await emit(
            "text_delta", {"stream_id": "preview-test", "delta": "先讲原理，再给例子。"}
        )
        await emit("message", final_message)
        operation_completed.set()
        return final_message

    response = await execute_operation(
        request, "session-test", delayed_operation, stream=True
    )
    tasks = list(request.app.state.tasks)
    try:
        assert isinstance(response, StreamingResponse)
        assert response.media_type == "text/event-stream"
        assert response.headers["x-accel-buffering"] == "no"
        first = decode_frame(await asyncio.wait_for(anext(response.body_iterator), 1))
        assert first["event"] == "text_delta"
        assert first["data"]["delta"] == "先讲原理，"
        assert not operation_completed.is_set()
        assert not release_tail.is_set()
        assert len(tasks) == 1 and not tasks[0].done()
        assert persisted == []

        release_tail.set()
        remaining = [decode_frame(frame) async for frame in response.body_iterator]
        assert [frame["event"] for frame in remaining] == [
            "text_reset",
            "text_delta",
            "message",
            "done",
        ]
        assert remaining[-2]["data"] == final_message
        assert remaining[-1]["data"]["status"] == "completed"
        assert operation_completed.is_set()
        assert persisted == []
        assert len({frame["id"] for frame in [first, *remaining]}) == 5
        assert remaining[-1]["id"].startswith(remaining[-1]["data"]["turn_id"])
        sessions.get.assert_called_once_with("session-test")
    finally:
        release_tail.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await response.body_iterator.aclose()
        # 已结束任务的 done_callback 会在下一次事件循环调度时回收引用。
        await asyncio.sleep(0)
    assert request.app.state.tasks == set()


@pytest.mark.asyncio
async def test_non_streaming_preserves_original_result(isolated_stream_request):
    request, persisted, sessions = isolated_stream_request
    original_result = {
        "session": {"id": "session-test"},
        "message": {"content": "完整结果"},
    }

    async def operation(turn_id, emit):
        await emit("text_delta", {"delta": "临时文字"})
        await emit("text_reset", {})
        await emit("message", original_result["message"])
        return original_result

    result = await execute_operation(request, "session-test", operation, stream=False)

    assert result is original_result
    assert persisted == []
    assert request.app.state.tasks == set()
    sessions.get.assert_called_once_with("session-test")


@pytest.mark.asyncio
async def test_stream_failure_keeps_error_and_closes_event_channel(
    isolated_stream_request,
):
    request, persisted, _ = isolated_stream_request

    async def failing_operation(turn_id, emit):
        await emit("text_delta", {"delta": "未完成的预览"})
        raise ValueError("模拟生成中断")

    response = await execute_operation(
        request, "session-test", failing_operation, stream=True
    )
    tasks = list(request.app.state.tasks)
    try:

        async def consume():
            return [decode_frame(frame) async for frame in response.body_iterator]

        frames = await asyncio.wait_for(consume(), 1)
        assert [frame["event"] for frame in frames] == ["text_delta", "error"]
        assert frames[-1]["data"]["error"] == "ValueError"
        assert frames[-1]["data"]["message"] == "模拟生成中断"
        assert persisted == []
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)
        await response.body_iterator.aclose()
        await asyncio.sleep(0)
    assert request.app.state.tasks == set()


@pytest.mark.asyncio
async def test_client_disconnect_keeps_operation_without_buffering_events(
    isolated_stream_request,
):
    request, persisted, _ = isolated_stream_request
    release = asyncio.Event()
    final_message = {"id": "after-disconnect", "role": "assistant", "content": "已完成正文"}

    async def operation(turn_id, emit):
        await emit("text_delta", {"stream_id": "preview", "delta": "已开始"})
        await release.wait()
        for _ in range(1000):
            await emit("text_delta", {"delta": "断线后不再缓冲"})
        await emit("message", final_message)
        return final_message

    response = await execute_operation(request, "session-test", operation, stream=True)
    tasks = list(request.app.state.tasks)
    try:
        first = decode_frame(await asyncio.wait_for(anext(response.body_iterator), 1))
        assert first["event"] == "text_delta"
        # 模拟浏览器断线；生产者不能因无人消费的队列而阻塞。
        await response.body_iterator.aclose()
        assert len(tasks) == 1 and not tasks[0].done()
        release.set()
        result = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        assert result == [final_message] and persisted == []
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await response.body_iterator.aclose()
        await asyncio.sleep(0)
    assert request.app.state.tasks == set()


@pytest.mark.asyncio
async def test_queue_overflow_ends_stream_but_not_business_operation(isolated_stream_request, monkeypatch):
    request, persisted, _ = isolated_stream_request
    monkeypatch.setattr("app.api.streaming.QUEUE_CAPACITY", 2)

    async def operation(turn_id, emit):
        for i in range(1000):
            await emit("text_delta", {"delta": str(i)})
        return {"completed": True}

    response = await execute_operation(request, "session-test", operation, stream=True)
    tasks = list(request.app.state.tasks)
    result = await asyncio.wait_for(asyncio.gather(*tasks), 1)
    frames = [frame async for frame in response.body_iterator]
    assert result == [{"completed": True}] and frames == [] and persisted == []


def test_historical_events_endpoint_is_explicitly_retired(isolated_stream_request):
    from fastapi import HTTPException
    from app.api.routes.sessions import events
    request, _, _ = isolated_stream_request
    with pytest.raises(HTTPException) as error:
        events("session-test", request)
    assert error.value.status_code == 410
