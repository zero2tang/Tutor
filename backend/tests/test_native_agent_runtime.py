"""使用真实 ChatOpenAI 和隔离 HTTP，验证原生循环而非旧 chat 协议替身。"""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from langchain.tools import tool, ToolRuntime
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict

from app.agent.runtime.contracts import PreparedRun, RuntimeContext
from app.agent.tools.contracts import ToolContext
from app.application.agent.runner import AgentRunner
from app.application.agent.tools import TeachingTools
from app.application.agent.summary import HistorySummary, SummarySourceMismatch, summarize_sources
from app.infrastructure.clients.llm import ModelClient


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    private: str = ""


class Recorder:
    def __init__(self):
        self.records = []

    def start(self, *args):
        self.records.append(("started", args[0]))

    def complete(self, run_id, duration, usage, trace, output):
        self.records.append(("completed", deepcopy(trace), dict(usage)))

    def fail(self, run_id, duration, usage, trace, error):
        self.records.append(("failed", error, deepcopy(trace), dict(usage)))


async def prepare(role, skill, payload, schema, scope, run_id, emit, use_tools, max_tokens, **options):
    return PreparedRun(payload=deepcopy(payload), instruction="只使用当前输入", purpose="teaching",
                       use_tools=use_tools, display_name="测试角色", public_fields=frozenset({"text"}))


def completion(name, arguments, *, identifier="out"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


def response(body, message):
    if body.get("response_format") == {"type": "json_object"}:
        message = {"role": "assistant", "content": message["tool_calls"][0]["function"]["arguments"]}
    usage = {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}
    envelope = {"id": "mock", "created": 1, "model": "test", "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}], "usage": usage}
    if not body.get("stream"):
        return httpx.Response(200, json=envelope)
    calls = message.get("tool_calls", [])
    frames = []
    for index, call in enumerate(calls):
        for part in (dict(name=call["function"]["name"], arguments=""), dict(arguments=call["function"]["arguments"])):
            frames.append({"id": "mock", "created": 1, "model": "test", "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"tool_calls": [{"index": index, "id": call["id"] if "name" in part else None,
                    "type": "function", "function": part}]}, "finish_reason": None}]})
    frames.extend([{"id": "mock", "created": 1, "model": "test", "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                   {"id": "mock", "created": 1, "model": "test", "object": "chat.completion.chunk", "choices": [], "usage": usage}])
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content="".join("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n" for frame in frames) + "data: [DONE]\n\n")


def client(handler, **settings):
    model = ModelClient.__new__(ModelClient)
    model.settings = SimpleNamespace(llm_model="test", llm_key="fake", llm_url="https://model.invalid/v1", **settings)
    model.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model.limit = asyncio.Semaphore(3)
    return model


class LookupTools:
    def __init__(self, function):
        self.value = function

    def for_role(self, role, *, handler=None):
        return [self.value]


@pytest.mark.asyncio
async def test_real_tool_returns_to_same_model_and_public_function_stream_only():
    requests, events, scopes = [], [], []

    @tool
    async def lookup(runtime: ToolRuntime[RuntimeContext]):
        """读取可信业务范围。"""
        scopes.append(runtime.context.scope)
        return {"value": 7}

    def http(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return response(body, completion("lookup", {}, identifier="lookup-1"))
        receipt = next(item for item in body["messages"] if item.get("tool_call_id") == "lookup-1")
        assert json.loads(receipt["content"]) == {"value": 7}
        return response(body, completion("Output", {"text": "结果为7", "private": "内部答案"}))

    async def emit(event, data):
        events.append((event, data))

    model, recorder = client(http), Recorder()
    scope = ToolContext(course_id="c", student_id="s", request_id="r")
    try:
        result = await AgentRunner(model, LookupTools(lookup), prepare_run=prepare, recorder=recorder).run(
            "worker", "auto", {"input": "求值"}, Output, scope, "turn", emit=emit)
    finally:
        await model.http.aclose()
    assert result["output"]["text"] == "结果为7" and scopes == [scope]
    assert len(requests) == 2 and "runtime" not in requests[0]["tools"][0]["function"]["parameters"]["properties"]
    assert "".join(item["delta"] for name, item in events if name == "text_delta") == "结果为7"
    assert result["usage"]["prompt_tokens"] == 40
    assert recorder.records[-1][1][0]["name"] == "lookup"


@pytest.mark.asyncio
async def test_real_delegation_tool_injects_full_config_and_context():
    calls, requests = [], []

    async def delegate(role, name, arguments, scope, *, runtime_context, config):
        calls.append((role, name, arguments, scope, runtime_context, config))
        return {"child_run_ids": ["child"], "message": "已讲解"}

    def http(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return response(body, completion("call_knowledge_agent", {"task": "解释", "concept_ids": ["c"]}, identifier="delegate"))
        assert "已讲解" in next(item["content"] for item in body["messages"] if item.get("tool_call_id") == "delegate")
        return response(body, completion("Output", {"text": "下一步", "private": ""}))

    model = client(http)
    try:
        result = await AgentRunner(model, TeachingTools(None), prepare_run=prepare, recorder=Recorder()).run(
            "supervisor", "auto", {}, Output, ToolContext(request_id="r"), "turn", tool_source=delegate)
    finally:
        await model.http.aclose()
    assert result["output"]["text"] == "下一步" and len(calls) == 1
    assert calls[0][4].run_id == result["run_id"] and "callbacks" in calls[0][5]


@pytest.mark.asyncio
async def test_forged_identity_and_unregistered_tool_are_rejected_without_effect():
    requests, effects = [], []

    @tool
    async def lookup(value: int, runtime: ToolRuntime[RuntimeContext]):
        """只能读取指定整数。"""
        effects.append(value)
        return {"value": value}

    def http(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return response(body, completion("lookup", {"value": 1, "student_id": "other"}, identifier="bad"))
        assert "request_rejected" in next(item["content"] for item in body["messages"] if item.get("tool_call_id") == "bad")
        return response(body, completion("Output", {"text": "拒绝", "private": ""}))

    model = client(http)
    try:
        await AgentRunner(model, LookupTools(lookup), prepare_run=prepare, recorder=Recorder()).run(
            "worker", "auto", {}, Output, ToolContext(request_id="r"), "turn")
    finally:
        await model.http.aclose()
    assert not effects


@pytest.mark.asyncio
async def test_summary_checks_source_aliases_with_native_structured_http():
    def http(request):
        body = json.loads(request.content)
        return response(body, completion("HistorySummary", {"source_refs": ["s0"], "confirmed_findings": ["事实"]}))

    model = client(http)
    try:
        value = await summarize_sources(model, [{"source_ref": "real", "data": "事实"}])
        assert value["source_refs"] == ["real"]
        with pytest.raises(SummarySourceMismatch):
            await summarize_sources(model, [{"source_ref": "first", "data": "事实"}, {"source_ref": "second", "data": "其他事实"}])
    finally:
        await model.http.aclose()


@pytest.mark.asyncio
async def test_attached_summary_middleware_replaces_history_but_preserves_current_task():
    requests = []

    @tool
    async def lookup(runtime: ToolRuntime[RuntimeContext]):
        """测试工具。"""
        return {"value": 1}

    def http(request):
        body = json.loads(request.content)
        requests.append(body)
        names = [item["function"]["name"] for item in body.get("tools", [])]
        if body.get("response_format"):
            data = json.loads(body["messages"][-2]["content"])
            return response(body, completion("HistorySummary", {"source_refs": data["allowed_source_refs"], "confirmed_findings": ["既往学习事实"]}))
        assert "不可裁剪原答" in body["messages"][-1]["content"]
        return response(body, completion("Output", {"text": "继续", "private": ""}))

    model = client(http, context_window_tokens=12000, context_request_tokens=12000,
                   context_safety_tokens=100, context_keep_recent_turns=1)
    previous = [HumanMessage(content=f"历史{index}：" + "此前已经完成的学习过程。" * 50, id=f"old-{index}") for index in range(14)]
    try:
        result = await AgentRunner(model, LookupTools(lookup), prepare_run=prepare, recorder=Recorder()).run(
            "worker", "auto", {"student_answer": "不可裁剪原答"}, Output, ToolContext(request_id="r"), "turn", messages=previous, max_tokens=300)
    finally:
        await model.http.aclose()
    assert len(requests) >= 2
    assert any(item.additional_kwargs.get("lc_source") == "summarization" for item in result["messages"])
    assert not any(item.id == "old-0" for item in result["messages"])


@pytest.mark.asyncio
async def test_sqlite_checkpoint_stores_asset_references_and_provider_receives_real_pixels(tmp_path):
    from langchain_core.runnables import RunnableConfig
    from langgraph.graph import StateGraph, MessagesState, START, END
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from app.application.agent.images import QuestionImageContext
    from test_question_images import PNG

    (tmp_path / "question_7-image_0.png").write_bytes(PNG)
    requests = []
    def http(request):
        body = json.loads(request.content)
        requests.append(body)
        assert any(isinstance(message.get("content"), list) and any(part.get("type") == "image_url"
                   for part in message["content"]) for message in body["messages"])
        return response(body, completion("Output", {"text": "已读取公开图像", "private": ""}))

    async def image_prepare(*args, **options):
        prepared = await prepare(*args, **options)
        prepared.image_context = QuestionImageContext("math", image_root=tmp_path)
        return prepared

    @tool
    async def lookup(runtime: ToolRuntime[RuntimeContext]):
        """不产生持久副作用的测试工具。"""
        return {"value": 1}

    model = client(http)
    runner = AgentRunner(model, LookupTools(lookup), prepare_run=image_prepare, recorder=Recorder())
    async def supervisor(state, config: RunnableConfig):
        result = await runner.run("worker", "auto", {"question": {"id": "math:7", "course_id": "math",
            "stem": "数图形", "images": [{"url": "/api/assets/math/question_7-image_0.png"}]}}, Output,
            ToolContext(course_id="math", session_id="s", request_id="r"), "turn", config=config)
        return {"messages": result["messages"]}

    path = tmp_path / "checkpoint.sqlite"
    try:
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            await saver.setup()
            graph = StateGraph(MessagesState)
            graph.add_node("supervisor", supervisor)
            graph.add_edge(START, "supervisor")
            graph.add_edge("supervisor", END)
            compiled = graph.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": "pixels"}, "metadata": {"tutor_durable": True}}
            result = await compiled.ainvoke({"messages": []}, config, durability="sync")
            assert "data:image" not in str(result)
            snapshot = await compiled.aget_state(config)
            assert "data:image" not in str(snapshot.values)
    finally:
        await model.http.aclose()
    assert requests and b"data:image" not in path.read_bytes()


@pytest.mark.asyncio
async def test_cached_role_agent_reuses_graph_without_reusing_student_context():
    from app.application.agent.graph import create_role_agent

    @tool
    async def lookup(runtime: ToolRuntime[RuntimeContext]):
        """读取当前请求身份。"""
        return {"student": runtime.context.scope.student_id}

    model = client(lambda request: response(json.loads(request.content), completion("Output", {"text": "ok"})))
    tools = [lookup]
    first = RuntimeContext(scope=ToolContext(student_id="alice"), role="worker", run_id="a", root_run_id="a")
    second = RuntimeContext(scope=ToolContext(student_id="bob"), role="worker", run_id="b", root_run_id="b")
    try:
        graph_a, events_a = create_role_agent(model, first, Output, "甲的教学规则", tools=tools, max_rounds=3, max_tokens=300)
        graph_b, events_b = create_role_agent(model, second, Output, "乙的教学规则", tools=tools, max_rounds=3, max_tokens=300)
        assert graph_a is graph_b and events_a is not events_b
        assert first.instruction == "甲的教学规则" and second.instruction == "乙的教学规则"
    finally:
        await model.http.aclose()


@pytest.mark.asyncio
async def test_mixed_final_and_business_call_reads_receipt_before_final_validation():
    requests, effects = [], []
    @tool
    async def lookup(runtime: ToolRuntime[RuntimeContext]):
        """执行必须先取得的真实查询。"""
        effects.append("done")
        return {"value": 7}
    def http(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            value = completion("lookup", {}, identifier="lookup")
            value["tool_calls"].extend(completion("Output", {"text": "过早提案"}, identifier="early")["tool_calls"])
            return response(body, value)
        assert any(item.get("tool_call_id") == "lookup" for item in body["messages"])
        return response(body, completion("Output", {"text": "读取后提案"}))
    def validate(value):
        assert effects == ["done"]
        assert value["text"] == "读取后提案"
    model = client(http)
    try:
        result = await AgentRunner(model, LookupTools(lookup), prepare_run=prepare, recorder=Recorder()).run(
            "worker", "auto", {}, Output, ToolContext(request_id="mixed"), "turn", validate_output=validate)
        assert result["output"]["text"] == "读取后提案"
    finally:
        await model.http.aclose()
