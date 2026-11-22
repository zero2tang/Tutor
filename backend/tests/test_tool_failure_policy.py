"""用真实异步调度验证同任务故障累计、并发熔断及错误分类。"""

import asyncio
import json

import pytest
from pydantic import BaseModel

from app.agent.runtime.contracts import RuntimeContext, RunBudget
from native_runtime_helpers import LoopState, run_agent_loop
from native_runtime_helpers import execute_tool_batch
from app.agent.runtime.tool_policy import ToolFailurePolicy
from app.shared.execution import LearningTurnLost
from app.agent.tools.errors import ToolRejected
from app.agent.tools.contracts import ToolContext
from test_agent_autonomy import ScriptedModel


def tool_call(index, name="lookup", **arguments):
    return {
        "id": f"failure-policy-{index}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps({"index": index, **arguments}),
        },
    }


class CallbackTools:
    def __init__(self, callback, validator=None):
        self.callback = callback
        self.validator = validator
        self.started = []

    def validate_call(self, role, name, arguments):
        if self.validator:
            self.validator(role, name, arguments)

    async def execute(self, role, name, arguments, context):
        self.started.append((name, arguments["index"]))
        return await self.callback(name, arguments)


class LoopResult(BaseModel):
    message: str


class LoopTools(CallbackTools):
    def definitions(self, role):
        # 提供非空的真实函数定义，避免只验证空工具列表时的退化路径。
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "本地故障策略测试工具",
                    "parameters": {
                        "type": "object",
                        "properties": {"index": {"type": "integer"}},
                        "required": ["index"],
                    },
                },
            }
            for name in ("broken", "healthy")
        ]


async def run_scripted_loop(tools, requested_calls, runtime_context=None):
    model = ScriptedModel(
        [{"tool_calls": [call]} for call in requested_calls]
        + [{"message": "已根据可用结果结束。"}]
    )
    state = LoopState()
    output = await asyncio.wait_for(
        run_agent_loop(
            model,
            tools,
            "supervisor",
            [],
            LoopResult,
            ToolContext(),
            state,
            runtime_context=runtime_context,
        ),
        timeout=3,
    )
    assert output == {"message": "已根据可用结果结束。"}
    return model, state


async def run_batch(calls, tools, policy, on_complete=None):
    # 测试替身只执行本地回调，不连接数据库、模型或网络。
    return await asyncio.wait_for(
        execute_tool_batch(
            calls,
            tools,
            "supervisor",
            ToolContext(),
            remaining_calls=32,
            loaded_skills=set(),
            max_concurrency=4,
            failure_policy=policy,
            on_complete=on_complete,
        ),
        timeout=3,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
async def test_one_failing_batch_executes_only_three_same_named_calls(error_type):
    policy = ToolFailurePolicy()
    three_started = asyncio.Event()
    active = 0
    peak = 0

    async def fail(name, arguments):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 3:
            three_started.set()
        try:
            await three_started.wait()
            raise error_type("本地工具故障")
        finally:
            active -= 1

    tools = CallbackTools(fail)
    outcomes = await run_batch([tool_call(i) for i in range(6)], tools, policy)

    assert len(tools.started) == 3
    assert peak == 3 and active == 0
    assert policy.failure_counts == {"lookup": 3}
    assert policy.is_blocked("lookup")
    assert [o.result["error_kind"] for o in outcomes] == [
        "execution_error",
        "execution_error",
        "execution_error",
        "circuit_open",
        "circuit_open",
        "circuit_open",
    ]
    assert all(o.trace["failure_limit"] == 3 for o in outcomes)
    assert all(o.trace["failure_count"] == 3 for o in outcomes[3:])
    assert all(o.trace["circuit_open"] for o in outcomes[3:])


@pytest.mark.asyncio
async def test_same_policy_accumulates_failures_across_batches():
    async def fail(name, arguments):
        raise RuntimeError("跨批故障")

    tools = CallbackTools(fail)
    policy = ToolFailurePolicy()
    await run_batch([tool_call(0), tool_call(1)], tools, policy)
    assert policy.failure_counts == {"lookup": 2}
    await run_batch([tool_call(2)], tools, policy)
    blocked = await run_batch([tool_call(3), tool_call(4)], tools, policy)

    assert len(tools.started) == 3
    assert all(o.result["error_kind"] == "circuit_open" for o in blocked)
    assert policy.failure_counts == {"lookup": 3}


@pytest.mark.asyncio
async def test_success_preserves_current_run_failure_count_and_trace():
    policy = ToolFailurePolicy()

    async def alternate(name, arguments):
        if arguments["index"] % 2 == 0:
            raise RuntimeError("间歇性故障")
        return {"value": arguments["index"]}, {"name": name, "status": "completed"}

    tools = CallbackTools(alternate)
    outcomes = []
    published = []

    async def completed(outcome):
        # 回调可将这一份完整轨迹直接交给运行记录持久化。
        published.append(dict(outcome.trace))

    for index in range(6):
        outcomes.extend(await run_batch([tool_call(index)], tools, policy, completed))

    assert len(tools.started) == 5
    assert [o.trace["failure_count"] for o in outcomes] == [1, 1, 2, 2, 3, 3]
    assert [
        o.trace["error_kind"] for o in outcomes if o.trace["status"] == "failed"
    ] == ["execution_error", "execution_error", "execution_error", "circuit_open"]
    assert policy.failure_counts == {"lookup":3}
    assert published == [o.trace for o in outcomes]
    assert outcomes[-1].result["error_kind"] == "circuit_open"


@pytest.mark.asyncio
async def test_blocked_tool_does_not_block_another_tool():
    policy = ToolFailurePolicy()
    for _ in range(3):policy.record_failure("broken")

    async def succeed(name, arguments):
        return {"value": arguments["index"]}, {"name": name, "status": "completed"}

    tools = CallbackTools(succeed)
    outcomes = await run_batch(
        [tool_call(0, "broken"), tool_call(1, "healthy"), tool_call(2, "healthy")],
        tools,
        policy,
    )

    assert tools.started == [("healthy", 1), ("healthy", 2)]
    assert outcomes[0].result["error_kind"] == "circuit_open"
    assert [o.result["value"] for o in outcomes[1:]] == [1, 2]
    assert policy.failure_counts == {"broken": 3}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "expected_kind"),
    [
        (ToolRejected, "quality_rejected"),
        (ValueError, "request_rejected"),
        (PermissionError, "request_rejected"),
    ],
)
async def test_quality_and_request_rejections_do_not_count_as_tool_faults(
    error_type, expected_kind
):
    async def reject(name, arguments):
        raise error_type("业务拒绝或参数错误")

    policy = ToolFailurePolicy()
    tools = CallbackTools(reject)
    outcomes = await run_batch([tool_call(i) for i in range(6)], tools, policy)

    assert len(tools.started) == 6
    assert policy.failure_counts == {}
    assert all(o.result["error_kind"] == expected_kind for o in outcomes)
    assert all(o.trace["failure_count"] == 0 for o in outcomes)
    assert not any(o.trace["circuit_open"] for o in outcomes)


@pytest.mark.asyncio
async def test_invalid_json_and_permission_validation_never_execute_or_count():
    async def unexpected(name, arguments):
        pytest.fail("拒绝的请求不应进入工具执行")

    def denied(role, name, arguments):
        raise PermissionError("角色无权调用")

    policy = ToolFailurePolicy()
    tools = CallbackTools(unexpected, validator=denied)
    calls = [tool_call(0), tool_call(1), tool_call(2)]
    calls[0]["function"]["arguments"] = "{"
    calls[1]["function"]["arguments"] = "[]"
    outcomes = await run_batch(calls, tools, policy)

    assert tools.started == [] and policy.failure_counts == {}
    assert all(o.result["error_kind"] == "request_rejected" for o in outcomes)


@pytest.mark.asyncio
async def test_returned_execution_failure_receipt_also_counts_and_opens_circuit():
    async def failed_receipt(name, arguments):
        return {"error": "DependencyUnavailable"}, {
            "name": name,
            "status": "failed",
            "error_kind": "execution_error",
        }

    policy = ToolFailurePolicy()
    tools = CallbackTools(failed_receipt)
    outcomes = await run_batch([tool_call(i) for i in range(6)], tools, policy)

    assert len(tools.started) == 3 and policy.failure_counts == {"lookup": 3}
    assert all(o.trace["error_kind"] == "execution_error" for o in outcomes[:3])
    assert all(o.result["error_kind"] == "circuit_open" for o in outcomes[3:])


@pytest.mark.asyncio
async def test_cancellation_waits_for_children_without_recording_fault():
    entered = asyncio.Event()
    active = 0

    async def wait_forever(name, arguments):
        nonlocal active
        active += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    policy = ToolFailurePolicy()
    tools = CallbackTools(wait_forever)
    task = asyncio.create_task(
        run_batch([tool_call(i) for i in range(6)], tools, policy)
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert active == 0 and policy.failure_counts == {}


@pytest.mark.asyncio
async def test_lost_turn_ownership_escapes_without_becoming_retryable_tool_error():
    async def lost(name, arguments):
        raise LearningTurnLost("任务已被新的执行接管")

    policy = ToolFailurePolicy()
    tools = CallbackTools(lost)
    with pytest.raises((ExceptionGroup,LearningTurnLost)) as caught:
        await run_batch([tool_call(0)], tools, policy)

    assert isinstance(caught.value,LearningTurnLost) or caught.value.subgroup(LearningTurnLost) is not None
    assert policy.failure_counts == {}


@pytest.mark.asyncio
async def test_different_tools_still_overlap_with_global_concurrency_four():
    four_started = asyncio.Event()
    active = 0
    peak = 0

    async def overlap(name, arguments):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 4:
            four_started.set()
        try:
            await four_started.wait()
            await asyncio.sleep(0)
            return {"value": arguments["index"]}, {"name": name, "status": "completed"}
        finally:
            active -= 1

    policy = ToolFailurePolicy()
    tools = CallbackTools(overlap)
    outcomes = await run_batch(
        [tool_call(i, f"lookup_{i}") for i in range(8)], tools, policy
    )

    assert peak == 4 and active == 0
    assert len(tools.started) == 8 and policy.failure_counts == {}
    assert [o.result["value"] for o in outcomes] == list(range(8))


@pytest.mark.asyncio
async def test_same_healthy_tool_has_three_in_flight_slots_without_losing_calls():
    three_started = asyncio.Event()
    active = 0
    peak = 0

    async def overlap(name, arguments):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 3:
            three_started.set()
        try:
            await three_started.wait()
            await asyncio.sleep(0)
            return {"value": arguments["index"]}, {"name": name, "status": "completed"}
        finally:
            active -= 1

    policy = ToolFailurePolicy()
    tools = CallbackTools(overlap)
    outcomes = await run_batch([tool_call(i) for i in range(7)], tools, policy)

    assert peak == 3 and active == 0
    assert len(tools.started) == 7 and policy.failure_counts == {}
    assert [o.result["value"] for o in outcomes] == list(range(7))


@pytest.mark.asyncio
async def test_loop_removes_failed_tool_definitions_but_keeps_healthy_tool_and_finishes():
    async def execute(name, arguments):
        if name == "broken":
            raise RuntimeError("连续运行故障")
        return {"value": arguments["index"]}, {"name": name, "status": "completed"}

    tools = LoopTools(execute)
    model, state = await run_scripted_loop(
        tools,
        [tool_call(i, "broken") for i in range(3)] + [tool_call(3, "healthy")],
    )

    names_by_request = [
        {item["function"]["name"] for item in kwargs["tools"] if not item["function"]["name"][0].isupper()}
        for _, kwargs in model.calls
    ]
    assert names_by_request[:3] == [{"broken", "healthy"}] * 3
    assert names_by_request[3:] == [{"healthy"}, {"healthy"}]
    assert tools.started == [
        ("broken", 0),
        ("broken", 1),
        ("broken", 2),
        ("healthy", 3),
    ]
    assert [trace["failure_count"] for trace in state.trace[:3]] == [1, 2, 3]
    assert state.trace[-1]["status"] == "completed"
    assert len(model.calls) == 5


@pytest.mark.asyncio
async def test_child_calls_share_explicit_root_budget_without_private_contextvars():
    async def fail(name, arguments):
        raise RuntimeError("同名工具故障")
    tools = LoopTools(fail)
    budget = RunBudget()
    def context(identifier, role):
        return RuntimeContext(scope=ToolContext(), role=role, run_id=identifier,
            root_run_id="root", budget=budget)
    root = context("root", "supervisor")
    await run_scripted_loop(tools, [tool_call(0, "broken")], root)
    assert budget.failures.failure_counts == {"broken": 1}
    child = context("child", "exercise")
    await run_scripted_loop(tools, [tool_call(1, "broken"), tool_call(2, "broken")], child)
    sibling = context("sibling", "knowledge")
    model, state = await run_scripted_loop(tools, [tool_call(3, "broken")], sibling)
    assert budget.failures.failure_counts == {"broken": 3}
    assert tools.started == [("broken", 0), ("broken", 1), ("broken", 2)]
    assert state.trace[0]["error_kind"] == "circuit_open"
    fresh = RuntimeContext(scope=ToolContext(), role="supervisor", run_id="new", root_run_id="new")
    await run_scripted_loop(tools, [tool_call(4, "broken")], fresh)
    assert fresh.tool_policy.failure_counts == {"broken": 1}
    assert budget.failures.failure_counts == {"broken": 3}
