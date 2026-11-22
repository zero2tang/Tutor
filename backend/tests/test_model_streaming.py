"""用异步 HTTP 流验证正文即时回调、响应聚合与断流处理，不访问远程模型。"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.infrastructure.clients.llm import ModelClient, ModelError


def sse(payload: dict | str) -> bytes:
    value = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {value}\n\n".encode()


def chunk(delta: dict, finish_reason: str | None = None) -> dict:
    return {
        "id": "chat-test", "object": "chat.completion.chunk", "created": 123,
        "model": "fake-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


class Pieces(httpx.AsyncByteStream):
    def __init__(self, pieces: list[bytes], error: Exception | None = None):
        self.pieces = pieces
        self.error = error

    async def __aiter__(self):
        for piece in self.pieces:
            yield piece
        if self.error is not None:
            raise self.error


@pytest.fixture
async def client(monkeypatch):
    # 直接注入无密钥的测试配置，避免读取工作区模型凭据或创建缓存目录。
    instance = ModelClient.__new__(ModelClient)
    instance.settings = SimpleNamespace(
        llm_url="https://model.invalid/v1", llm_key="test-only", llm_model="fake-model"
    )
    instance.limit = asyncio.Semaphore(3)
    instance.http = httpx.AsyncClient(trust_env=False)

    async def immediate_sleep(_seconds):
        pass

    monkeypatch.setattr("app.infrastructure.clients.llm.asyncio.sleep", immediate_sleep)
    yield instance
    await instance.close()


async def use_transport(client, handler):
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)


async def test_content_callback_arrives_before_last_upstream_packet(client):
    emitted: list[str] = []
    callback_received = asyncio.Event()
    request_bodies = []

    class GatedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse(chunk({"role": "assistant", "reasoning_content": "内部推理"}))
            yield sse(chunk({"content": "你好"}))
            # 上游末包只会在回调后发送；先缓冲整个响应的实现会超时。
            await asyncio.wait_for(callback_received.wait(), timeout=1)
            yield sse(chunk({"content": "，同学"}))
            yield sse(chunk({}, "stop"))
            yield sse("[DONE]")

    async def handler(request):
        request_bodies.append(json.loads(request.content))
        return httpx.Response(200, stream=GatedStream())

    async def on_content(value):
        emitted.append(value)
        callback_received.set()

    await use_transport(client, handler)
    response = await client.chat([{"role": "user", "content": "你好"}], on_content=on_content)
    assert emitted == ["你好", "，同学"]
    assert request_bodies[0]["stream"] is True
    assert request_bodies[0]["stream_options"] == {"include_usage": True}
    assert response["object"] == "chat.completion"
    metrics = response["_request_metrics"]
    assert metrics["thinking"] == "disabled"
    assert metrics["stream"] is True
    assert metrics["request_attempts"] == 1
    assert 0 <= metrics["queue_wait_ms"] <= metrics["duration_ms"]
    assert 0 <= metrics["first_delta_ms"] <= metrics["first_content_ms"]
    assert metrics["first_content_ms"] <= metrics["duration_ms"]
    assert response["choices"][0] == {
        "index": 0,
        "message": {"role": "assistant", "content": "你好，同学", "reasoning_content": "内部推理"},
        "finish_reason": "stop",
    }


async def test_fragmented_tool_calls_reasoning_and_final_usage_are_aggregated(client):
    emitted = []
    pieces = [
        b": heartbeat\r\n\r\n",
        sse(chunk({"reasoning_content": "先"})),
        sse(chunk({"reasoning_content": "查题", "tool_calls": [
            {"index": 1, "id": "call_", "type": "function", "function": {"name": "find_"}},
            {"index": 0, "id": "first", "function": {"name": "lookup", "arguments": "{"}},
        ]})),
        sse(chunk({"tool_calls": [
            {"index": 1, "id": "2", "function": {"name": "question", "arguments": '{"q":'}},
            {"index": 0, "function": {"arguments": "}"}},
        ]})),
        sse(chunk({"tool_calls": [{"index": 1, "function": {"arguments": '"数学"}'}}]})),
        sse(chunk({}, "tool_calls")),
        sse({"choices": [], "usage": {
            "prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17,
            "prompt_cache_hit_tokens": 6, "prompt_cache_miss_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 6},
            "completion_tokens_details": {"reasoning_tokens": 0},
        }}),
        sse("[DONE]"),
    ]

    async def handler(_request):
        # 拆开 UTF-8 字节和事件边界，确保解析不依赖每次网络读取恰好一个事件。
        wire = b"".join(pieces)
        return httpx.Response(200, stream=Pieces([
            wire[offset:offset + 7] for offset in range(0, len(wire), 7)
        ]))

    async def on_content(value):
        emitted.append(value)

    await use_transport(client, handler)
    response = await client.chat([], tools=[{"type": "function"}], on_content=on_content)
    message = response["choices"][0]["message"]
    assert emitted == []
    assert message["content"] is None
    assert message["reasoning_content"] == "先查题"
    assert message["tool_calls"] == [
        {"id": "first", "type": "function", "function": {"name": "lookup", "arguments": "{}"}},
        {"id": "call_2", "type": "function",
         "function": {"name": "find_question", "arguments": '{"q":"数学"}'}},
    ]
    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert response["usage"] == {
        "prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17,
        "prompt_cache_hit_tokens": 6, "prompt_cache_miss_tokens": 4,
        "prompt_tokens_details": {"cached_tokens": 6},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }
    metrics = response["_request_metrics"]
    assert metrics["prompt_cache_hit_tokens"] == 6
    assert metrics["prompt_cache_miss_tokens"] == 4
    assert metrics["reasoning_tokens"] == 0
    assert metrics["first_delta_ms"] is not None
    assert metrics["first_content_ms"] is None


@pytest.mark.parametrize("partial", [
    [chunk({"content": "部分答案"})],
    [chunk({"content": "部分答案"}), chunk({}, "stop")],
    [chunk({"content": "部分答案"}), "[DONE]"],
])
async def test_incomplete_stream_is_error_and_never_replayed(client, partial):
    attempts = []
    emitted = []

    async def handler(_request):
        attempts.append(True)
        return httpx.Response(200, stream=Pieces([sse(piece) for piece in partial]))

    async def on_content(value):
        emitted.append(value)

    await use_transport(client, handler)
    with pytest.raises(ModelError, match="未完整结束"):
        await client.chat([], on_content=on_content)
    assert len(attempts) == 1
    assert emitted == ["部分答案"]


@pytest.mark.parametrize("delta", [
    {"content": "答案"}, {"reasoning_content": "内部推理"},
    {"tool_calls": [{"index": 0, "function": {"arguments": "{"}}]},
    {"role": "assistant"},
])
async def test_transport_failure_after_any_increment_is_not_retried(client, delta):
    attempts = []

    async def handler(_request):
        attempts.append(True)
        return httpx.Response(200, stream=Pieces([sse(chunk(delta))], httpx.ReadError("断开")))

    async def on_content(_value):
        pass

    await use_transport(client, handler)
    with pytest.raises(ModelError, match="ReadError"):
        await client.chat([], on_content=on_content)
    assert len(attempts) == 1


@pytest.mark.parametrize("first_failure", ["connect", "read", "status"])
async def test_temporary_failure_before_any_increment_can_retry(client, first_failure):
    attempts = []

    async def handler(_request):
        attempts.append(True)
        if len(attempts) == 1:
            if first_failure == "connect":
                raise httpx.ConnectError("暂时不可用")
            if first_failure == "read":
                return httpx.Response(200, stream=Pieces([], httpx.ReadError("暂时不可用")))
            return httpx.Response(503)
        return httpx.Response(200, stream=Pieces([
            sse(chunk({"content": "成功"})), sse(chunk({}, "stop")), sse("[DONE]"),
        ]))

    async def on_content(_value):
        pass

    await use_transport(client, handler)
    response = await client.chat([], on_content=on_content)
    assert len(attempts) == 2
    assert response["choices"][0]["message"]["content"] == "成功"
    assert response["_request_metrics"]["request_attempts"] == 2


async def test_chat_without_callback_keeps_non_streaming_contract(client):
    expected = {
        "choices": [{"message": {"role": "assistant", "content": "整段回答"}}],
        "usage": {
            "prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17,
            "prompt_cache_hit_tokens": 6, "prompt_cache_miss_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }

    async def handler(request):
        body = json.loads(request.content)
        assert body["stream"] is False
        assert "stream_options" not in body
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json=expected)

    await use_transport(client, handler)
    response = await client.chat([], json_output=True)
    metrics = response.pop("_request_metrics")
    assert response == expected
    assert metrics["stream"] is False
    assert metrics["first_delta_ms"] is None
    assert metrics["first_content_ms"] is None
    assert metrics["prompt_cache_hit_tokens"] == 6
    assert metrics["prompt_cache_miss_tokens"] == 4
    assert metrics["reasoning_tokens"] == 0


@pytest.mark.parametrize("thinking_options", [{}, {"thinking": None}, {"thinking": False},
                                              {"thinking": True}],
                         ids=["omitted", "none", "false", "legacy-true"])
@pytest.mark.parametrize("mode_options", [
    {}, {"json_output": True},
    {"tools": [{"type": "function", "function": {"name": "lookup"}}]},
], ids=["plain", "json", "tools"])
@pytest.mark.parametrize("streaming", [False, True], ids=["complete", "stream"])
async def test_all_chat_request_modes_explicitly_disable_thinking(
    client, thinking_options, mode_options, streaming,
):
    # 验证实际 HTTP 请求，而非仅检查默认参数；旧调用显式传 True 也不能重开。
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["stream"]:
            return httpx.Response(200, stream=Pieces([
                sse(chunk({"content": "回答"})), sse(chunk({}, "stop")), sse("[DONE]"),
            ]))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "回答"}}],
        })

    async def on_content(_value):
        pass

    await use_transport(client, handler)
    response = await client.chat(
        [{"role": "user", "content": "测试"}],
        **thinking_options,
        **mode_options,
        on_content=on_content if streaming else None,
    )
    assert len(requests) == 1
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["stream"] is streaming
    metrics = response["_request_metrics"]
    assert metrics["thinking"] == requests[0]["thinking"]["type"]
    assert metrics["stream"] is streaming
    assert metrics["model"] == "fake-model"
    assert metrics["prompt_cache_hit_tokens"] is None
    assert metrics["prompt_cache_miss_tokens"] is None
    assert metrics["reasoning_tokens"] is None
    assert set(metrics) == {
        "model", "thinking", "stream", "duration_ms", "queue_wait_ms", "request_attempts",
        "first_delta_ms", "first_content_ms", "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens", "reasoning_tokens",
    }
    if mode_options.get("tools"):
        assert requests[0]["tools"] == mode_options["tools"]
        assert requests[0]["tool_choice"] == "auto"
    elif mode_options.get("json_output"):
        assert requests[0]["response_format"] == {"type": "json_object"}
