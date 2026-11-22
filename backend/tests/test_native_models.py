"""通过标准 ChatOpenAI 和模拟 HTTP 验证原生纯JSON调用；不使用脚本模型兼容出口。"""

import asyncio
import json

import httpx
import pytest

from native_runtime_helpers import LoopState, run_agent_loop
from app.infrastructure.clients.chat import create_chat_model, invoke_structured
from app.infrastructure.clients.llm import ModelError
from app.shared.contracts import StrictModel
from app.shared.llm_budget import ContextBudgetExceeded, ProviderContextLimitError
from test_model_streaming import client, use_transport, sse, chunk, Pieces


class Result(StrictModel):
    answer: int
    feedback: str
    private: str = ""


def completion(value, *, finish="stop", usage=True):
    result = {"id": "native-1", "object": "chat.completion", "created": 123,
              "model": "fake-model", "choices": [{"index": 0,
              "message": {"role": "assistant", "content": json.dumps(value, ensure_ascii=False)}, "finish_reason": finish}]}
    if usage:
        result["usage"] = usage if isinstance(usage, dict) else {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    return httpx.Response(200, json=result)


@pytest.mark.asyncio
@pytest.mark.parametrize("value,valid", [
    ({"answer": 2, "feedback": "正确"}, True),
    ({"answer": "2", "feedback": "错误类型"}, False),
    ({"answer": True, "feedback": "错误类型"}, False),
    ({"answer": 2, "feedback": "正确", "unknown": 3}, False),
])
async def test_native_json_content_is_strict_and_budgeted(client, value, valid):
    bodies = []

    async def handler(request):
        bodies.append(json.loads(request.content))
        return completion(value)

    await use_transport(client, handler)
    result = await invoke_structured(client, [{"role": "user", "content": "计算"}], Result)
    assert (result["parsing_error"] is None) is valid
    assert isinstance(create_chat_model(client), __import__("langchain_openai").ChatOpenAI)
    assert bodies[0]["thinking"] == {"type": "disabled"}
    assert bodies[0]["response_format"] == {"type": "json_object"}
    assert "tools" not in bodies[0] and "tool_choice" not in bodies[0]
    assert json.dumps(Result.model_json_schema(), ensure_ascii=False) in bodies[0]["messages"][-1]["content"]
    assert result["raw"].usage_metadata["input_tokens"] == 5
    assert result["request_metrics"]["request_attempts"] == 1


@pytest.mark.asyncio
async def test_model_factory_reuses_clients_but_isolates_request_options(client):
    first = create_chat_model(client, max_tokens=500, streaming=True)
    second = create_chat_model(client, max_tokens=1000, streaming=False)
    assert first.root_client is second.root_client
    assert first.root_async_client is second.root_async_client
    assert (first.max_tokens, first.streaming) == (500, True)
    assert (second.max_tokens, second.streaming) == (1000, False)
    await client.close()
    assert client.http.is_closed


@pytest.mark.asyncio
async def test_markdown_or_extra_json_text_is_not_silently_repaired(client):
    response = completion({"answer": 2, "feedback": "ok"})
    value = json.loads(response.content)
    value["choices"][0]["message"]["content"] += "}"
    await use_transport(client, lambda request: httpx.Response(200, json=value))
    result = await invoke_structured(client, ["计算"], Result)
    assert result["parsed"] is None and result["parsing_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["length", "content_filter", None])
async def test_incomplete_response_is_not_accepted_or_retried(client, finish):
    calls = []

    async def handler(request):
        calls.append(1)
        return completion({"answer": 2, "feedback": "ok"}, finish=finish)

    await use_transport(client, handler)
    with pytest.raises(ModelError, match="不完整"):
        await invoke_structured(client, ["计算"], Result)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [False, {"prompt_tokens": 5}, {"completion_tokens": 3},
                                  {"prompt_tokens": 0, "completion_tokens": 3}])
async def test_missing_usage_remains_unknown(client, usage):
    await use_transport(client, lambda request: completion({"answer": 2, "feedback": "ok"}, usage=usage))
    result = await invoke_structured(client, ["计算"], Result)
    assert result["usage_complete"] is False
    assert result["context_metrics"]["estimated_minus_actual_tokens"] is None
    assert result["request_metrics"]["prompt_cache_hit_tokens"] is None
    state = LoopState()
    await run_agent_loop(client, None, "assessment", ["计算"], Result, None, state, use_tools=False)
    assert state.usage["_incomplete"] is True


@pytest.mark.asyncio
async def test_final_guard_counts_schema_before_sending(client):
    from types import SimpleNamespace

    client.settings = SimpleNamespace(**vars(client.settings), context_request_tokens=2400)
    calls = []
    await use_transport(client, lambda request: calls.append(1))
    with pytest.raises(ContextBudgetExceeded):
        await invoke_structured(client, [{"role": "user", "content": "计算"}], Result, max_tokens=300)
    assert not calls


@pytest.mark.asyncio
async def test_context_error_is_sanitized_and_not_retried(client):
    calls = []

    async def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"code": "context_length_exceeded", "message": "SECRET student input"}})

    await use_transport(client, handler)
    with pytest.raises(ProviderContextLimitError) as caught:
        await invoke_structured(client, ["计算"], Result)
    assert "SECRET" not in str(caught.value) and len(calls) == 1


@pytest.mark.asyncio
async def test_native_json_stream_exposes_only_public_fields(client):
    events, bodies = [], []
    received = asyncio.Event()

    class Gated(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse(chunk({"role": "assistant", "content": '{"feedback":"先到正文'}))
            await asyncio.wait_for(received.wait(), 2)
            yield sse(chunk({"content": '\",\"answer\":2,\"private\":\"私有答案\"}'}))
            yield sse(chunk({}, "stop"))
            yield sse({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})
            yield sse("[DONE]")

    async def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, stream=Gated(), headers={"content-type": "text/event-stream"})

    async def emit(name, data):
        events.append((name, data))
        if name == "text_delta":
            received.set()

    await use_transport(client, handler)
    result = await run_agent_loop(client, None, "assessment", [{"role": "user", "content": "计算"}],
        Result, None, LoopState(), use_tools=False, emit=emit, public_fields={"feedback"})
    assert result["answer"] == 2 and result["private"] == "私有答案"
    assert "".join(value["delta"] for name, value in events if name == "text_delta") == "先到正文"
    assert "私有答案" not in json.dumps(events, ensure_ascii=False)
    assert bodies[0]["stream"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", [{"reasoning_content": "private"}, {"content": "private"},
    {"tool_calls": [{"index": 0, "id": "out", "type": "function", "function": {"name": "Result", "arguments": "{"}}]}])
async def test_stream_disconnect_after_any_increment_never_retries(client, delta):
    calls = []

    async def handler(request):
        calls.append(1)
        return httpx.Response(200, stream=Pieces([sse(chunk(delta))], httpx.ReadError("cut")))

    async def content(value):
        pass

    await use_transport(client, handler)
    with pytest.raises(ModelError):
        await invoke_structured(client, ["计算"], Result, on_content=content)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancellation_releases_the_shared_provider_slot(client):
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.Event().wait()

    await use_transport(client, handler)
    task = asyncio.create_task(invoke_structured(client, ["计算"], Result))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.limit._value == 3


@pytest.mark.asyncio
async def test_new_and_legacy_requests_share_one_concurrency_limit(client):
    from app.infrastructure.clients.llm import ModelClient

    active, maximum = 0, 0
    full = asyncio.Event()

    async def handler(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 3:
            full.set()
        await asyncio.wait_for(full.wait(), 2)
        active -= 1
        return completion({"answer": 2, "feedback": "ok"})

    await use_transport(client, handler)
    await asyncio.gather(*[
        invoke_structured(client, ["计算"], Result) if i % 2 else ModelClient.chat(client, [{"role": "user", "content": "计算"}])
        for i in range(8)
    ])
    assert maximum == 3


@pytest.mark.asyncio
async def test_invalid_schema_repairs_are_bounded_without_legacy_agent_fallback(client, monkeypatch):
    calls = []

    async def handler(request):
        calls.append(json.loads(request.content))
        return completion({"answer": "2", "feedback": "错误类型不能静默转换"})

    def forbidden(*args, **kwargs):
        raise AssertionError("纯结构化路径不能进入旧 Agent 或 chat 传输")

    monkeypatch.setattr("app.application.agent.graph.create_agent", forbidden)
    monkeypatch.setattr(client, "chat", forbidden)
    await use_transport(client, handler)
    state = LoopState()
    with pytest.raises(ValueError, match="三次"):
        await run_agent_loop(client, None, "assessment", [{"role": "user", "content": "计算"}],
                             Result, None, state, use_tools=False)
    assert len(calls) == 3
    assert state.usage == {"prompt_tokens": 15, "completion_tokens": 9}


@pytest.mark.asyncio
async def test_malformed_json_repair_keeps_exact_rejected_content_and_feedback(client):
    bodies = []

    async def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        response = completion({"answer": 2, "feedback": "候选"})
        if len(bodies) == 1:
            value = json.loads(response.content)
            value["choices"][0]["message"]["content"] += "}"
            return httpx.Response(200, json=value)
        return response

    await use_transport(client, handler)
    result = await run_agent_loop(client, None, "assessment", [{"role": "user", "content": "计算"}],
        Result, None, LoopState(), use_tools=False)
    assert result["answer"] == 2 and len(bodies) == 2
    messages = bodies[1]["messages"]
    assert messages[-3]["role"] == "assistant" and messages[-3]["content"].endswith("}}")
    assert messages[-2]["role"] == "user" and "json_invalid" in messages[-2]["content"]
    assert messages[-1]["role"] == "system" and "JSON Schema" in messages[-1]["content"]
