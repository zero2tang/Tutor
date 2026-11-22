"""用事件屏障验证实际重叠执行、协议对应和只读工具的线程边界。"""

import asyncio
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from native_runtime_helpers import LoopState, run_agent_loop
from native_runtime_helpers import execute_tool_batch
from app.domains.learning.schemas import Plan
from app.shared.concurrency import run_blocking
from app.agent.tools.contracts import ToolContext
from app.application.agent.tools import TeachingTools
from test_agent_autonomy import ScriptedModel, autonomous_database, final


def call(index, name="lookup", arguments=None):
    return {
        "id": f"call-{index}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(
                arguments if arguments is not None else {"index": index}
            ),
        },
    }


class OverlappingTools:
    def __init__(self, width=2, fail=None):
        self.ready = asyncio.Event()
        self.width, self.fail = width, fail
        self.active, self.peak = 0, 0
        self.started, self.finished = [], []

    def definitions(self, role):
        return []

    async def execute(self, role, name, arguments, context):
        index = arguments["index"]
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.append(index)
        context.concept_ids.append(f"private-{index}")
        try:
            if len(self.started) >= self.width:
                self.ready.set()
            await self.ready.wait()
            await asyncio.sleep(0)
            if index == self.fail:
                raise RuntimeError("测试工具失败")
            return {"value": index, "citations": [{"id": f"source-{index}"}]}, {
                "name": name,
                "status": "completed",
            }
        finally:
            self.active -= 1
            self.finished.append(index)


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 4])
async def test_parallel_execution_is_bounded_and_context_is_isolated(width):
    # 同名调用预留故障名额；不同工具仍由全局并发上限约束。
    same_tool_width = min(width, 3)
    tools = OverlappingTools(same_tool_width)
    context = ToolContext(concept_ids=["math:c1"])
    results = await asyncio.wait_for(
        execute_tool_batch(
            [call(i) for i in range(7)],
            tools,
            "supervisor",
            context,
            remaining_calls=16,
            loaded_skills=set(),
            max_concurrency=width,
        ),
        timeout=3,
    )
    assert tools.peak == same_tool_width and tools.active == 0
    assert [result.result["value"] for result in results] == list(range(7))
    assert context.concept_ids == ["math:c1"]


@pytest.mark.asyncio
async def test_out_of_order_completion_preserves_tool_ids_and_messages():
    release = asyncio.Event()
    tools = OverlappingTools()
    events = []

    async def execute(role, name, arguments, context):
        index = arguments["index"]
        if index == 0:
            await release.wait()
        else:
            release.set()
        return {"value": index, "citations": [{"id": str(index)}]}, {
            "name": name,
            "status": "completed",
        }

    tools.execute = execute
    model = ScriptedModel([{"tool_calls": [call(0), call(1)]}, final()])
    state = LoopState()

    async def emit(event, data):
        if event == "tool_completed": events.append(data["tool_call_id"])

    await asyncio.wait_for(
        run_agent_loop(
            model, tools, "supervisor", [], Plan, ToolContext(), state, emit=emit
        ),
        timeout=3,
    )
    receipts = [message for message in model.calls[-1][0] if message["role"] == "tool"]
    assert events == ["call-1", "call-0"]
    assert [message["tool_call_id"] for message in receipts] == ["call-0", "call-1"]
    assert [json.loads(message["content"])["value"] for message in receipts] == [0, 1]
    assert {citation["id"] for citation in state.citations} == {"0", "1"}
    assert not [item for item in model.calls[0][0] if item["role"] != "system"]


@pytest.mark.asyncio
async def test_one_failure_does_not_cancel_other_tools():
    tools = OverlappingTools(width=3, fail=1)
    results = await asyncio.wait_for(
        execute_tool_batch(
            [call(i) for i in range(3)],
            tools,
            "supervisor",
            ToolContext(),
            remaining_calls=16,
            loaded_skills=set(),
        ),
        timeout=3,
    )
    assert results[1].result["error"] == "RuntimeError"
    assert results[0].result["value"] == 0 and results[2].result["value"] == 2
    assert len(tools.finished) == 3


@pytest.mark.asyncio
async def test_budget_is_reserved_before_scheduling_and_every_call_gets_receipt():
    tools = OverlappingTools(width=2)
    model = ScriptedModel([{"tool_calls": [call(i) for i in range(5)]}, final()])
    state = LoopState()
    await asyncio.wait_for(
        run_agent_loop(
            model, tools, "supervisor", [], Plan, ToolContext(), state, max_calls=2
        ),
        timeout=3,
    )
    assert tools.started == [0, 1]
    receipts = [message for message in model.calls[-1][0] if message["role"] == "tool"]
    assert len(receipts) == 5
    assert all(
        "预算" in json.loads(message["content"])["message"] for message in receipts[2:]
    )
    assert all(item["function"]["name"][0].isupper() for item in model.calls[-1][1]["tools"]) and len(state.trace) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_first", [False, True])
async def test_same_round_skill_reads_coalesce_without_bypassing_validation(
    monkeypatch, fail_first
):
    registry = TeachingTools(SimpleNamespace())
    original = registry.skills.read
    count = 0

    def read(role, name):
        nonlocal count
        count += 1
        if fail_first and count == 1:
            raise ValueError("测试首次读取失败")
        return original(role, name)

    monkeypatch.setattr(registry.skills, "read", read)
    calls = [call(i, "read_skill", {"name": "goal_selection"}) for i in range(3)]
    calls.append(
        call(3, "read_skill", {"name": "goal_selection", "course_id": "mechanics"})
    )
    loaded = set()
    results = await execute_tool_batch(
        calls,
        registry,
        "supervisor",
        ToolContext(),
        remaining_calls=16,
        loaded_skills=loaded,
    )
    assert count == (2 if fail_first else 1)
    assert sum("instructions" in result.result for result in results) == 1
    assert results[2].trace["cached"] and results[3].result["error"] == "ValueError"
    assert loaded == {"goal_selection"}


@pytest.mark.asyncio
async def test_invalid_arguments_and_denied_tool_do_not_block_valid_call():
    registry = TeachingTools(SimpleNamespace())
    calls = [call(0, "commit_student_state", {}), call(1, "read_skill", {})]
    calls[1]["function"]["arguments"] = "[1,2]"
    calls.append(call(2, "read_skill", {"name": "foundational"}))
    results = await execute_tool_batch(
        calls,
        registry,
        "knowledge",
        ToolContext(),
        remaining_calls=16,
        loaded_skills=set(),
    )
    assert [result.trace["status"] for result in results] == [
        "failed",
        "failed",
        "completed",
    ]
    assert results[0].result["error"] == "PermissionError"
    assert "instructions" in results[2].result


@pytest.mark.asyncio
async def test_database_tools_run_on_distinct_workers_with_independent_connections(
    autonomous_database, monkeypatch
):
    registry = TeachingTools(SimpleNamespace())
    barrier = threading.Barrier(2, timeout=3)
    threads = set()
    original = registry.catalog.course

    def course(course_id):
        threads.add(threading.get_ident())
        barrier.wait()
        return original(course_id)

    monkeypatch.setattr(registry.catalog, "course", course)
    results = await asyncio.wait_for(
        execute_tool_batch(
            [
                call(0, "get_course_overview", {}),
                call(1, "read_student_state", {}),
            ],
            registry,
            "supervisor",
            ToolContext(course_id="math", concept_ids=["math:c1"]),
            remaining_calls=16,
            loaded_skills=set(),
        ),
        timeout=5,
    )
    assert len(threads) == 2 and threading.get_ident() not in threads
    assert all(result.trace["status"] == "completed" for result in results)
    assert results[1].result["states"][0]["concept_id"] == "math:c1"


@pytest.mark.asyncio
async def test_async_retrieval_clients_stay_on_event_loop_and_overlap(
    autonomous_database,
):
    started = 0
    ready = asyncio.Event()
    thread = threading.get_ident()

    async def retrieve(*args, **kwargs):
        nonlocal started
        assert threading.get_ident() == thread
        started += 1
        if started == 2:
            ready.set()
        await ready.wait()
        return {"items": [], "channels_used": ["fixture"]}

    registry = TeachingTools(
        SimpleNamespace(retrieve=retrieve, graph=SimpleNamespace(expand=retrieve))
    )
    results = await asyncio.wait_for(
        execute_tool_batch(
            [
                call(0, "rag_retrieve", {"query": "计数"}),
                call(1, "get_knowledge_structure", {"concept_ids": ["math:c1"]}),
            ],
            registry,
            "knowledge",
            ToolContext(course_id="math", concept_ids=["math:c1"]),
            remaining_calls=16,
            loaded_skills=set(),
        ),
        timeout=5,
    )
    assert started == 2 and all(
        result.trace["status"] == "completed" for result in results
    )


@pytest.mark.asyncio
async def test_cancellation_waits_for_all_async_children_and_preserves_finished_trace():
    ready = asyncio.Event()
    first_completed = asyncio.Event()
    active = 0
    tools = OverlappingTools()

    async def execute(role, name, arguments, context):
        nonlocal active
        if arguments["index"] == 0:
            return {"value": 0}, {"name": name, "status": "completed"}
        active += 1
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    tools.execute = execute
    model = ScriptedModel([{"tool_calls": [call(0), call(1)]}])
    state = LoopState()
    async def emit(event, data):
        if event == "tool_completed" and data["tool_call_id"] == "call-0":
            first_completed.set()
    task = asyncio.create_task(
        run_agent_loop(model, tools, "supervisor", [], Plan, ToolContext(), state, emit=emit)
    )
    await asyncio.wait_for(ready.wait(), timeout=2)
    await asyncio.wait_for(first_completed.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0 and state.trace[0]["tool_call_id"] == "call-0"
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_blocking_worker_closes_before_cancellation_returns():
    started, release, closed = threading.Event(), threading.Event(), threading.Event()

    def query():
        started.set()
        try:
            assert release.wait(timeout=3)
        finally:
            closed.set()

    task = asyncio.create_task(run_blocking(query))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ids", [[None, "ok"], ["same", "same"]])
async def test_invalid_call_ids_are_rejected_before_any_execution(bad_ids):
    tools = OverlappingTools()
    calls = [call(i) for i in range(2)]
    for item, identifier in zip(calls, bad_ids):
        item["id"] = identifier
    with pytest.raises(ValueError, match="ID"):
        await execute_tool_batch(
            calls,
            tools,
            "supervisor",
            ToolContext(),
            remaining_calls=16,
            loaded_skills=set(),
        )
    assert tools.started == []
