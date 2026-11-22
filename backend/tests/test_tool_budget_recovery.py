"""正式输入行保存委派预算，完成消息保存回执；失败重试不产生第二份执行账本。"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agent.runtime.contracts import RuntimeContext
from app.domains.sessions.models import LearningSession, Message
from app.domains.learning.schemas import MessageRequest
from app.application.learning.delegation import LearningDelegation
from app.application.learning.receipts import receive, operation_id
from app.domains.practice.batches import ExerciseBatchService
from app.shared.execution import LearningTurnLost
from app.agent.tools.errors import ToolRejected
from app.agent.runtime.tool_policy import is_execution_error, raise_ownership_loss
from app.agent.tools.contracts import ToolContext
from test_agent_autonomy import learning_with
from test_exercise_context import exercise_database


async def emit(*args): pass


def context():
    return ToolContext(course_id="math", session_id="mine", student_id="local", concept_ids=["math:c1"])


def arguments(task="讲解计数限制"):
    return {"task": task, "concept_ids": ["math:c1"]}


def start_delegation(service, database, *, request_id="budget-request-1", turn_id="budget-turn-1"):
    snapshot = service.sessions.get("mine")
    state = service.claim("mine", turn_id)
    request = MessageRequest(content="讲解计数限制", request_id=request_id)
    with database() as db:
        receive(db, "mine", turn_id, {"kind": "message", **request.model_dump(exclude_unset=True)})
        from app.domains.memory.context import invalidation_epoch
        state["memory_epoch"] = invalidation_epoch(db, state["student_id"])
    return LearningDelegation(service, state, snapshot, request, turn_id, emit, database)


def budget(database, request_id="budget-request-1"):
    with database() as db:
        row = db.get(Message, operation_id("mine", request_id, "input"))
        return deepcopy(row.metadata_json.get("_native_budget", {}))


def parent(identifier="parent"):
    return RuntimeContext(scope=context(), role="supervisor", run_id=identifier, root_run_id=identifier)


def test_failed_distinct_tasks_reserve_eight_before_execution(exercise_database, monkeypatch):
    service, _ = learning_with([])
    delegation = start_delegation(service, exercise_database)
    effects = []
    async def reject(task, child, **options):
        effects.append(task)
        assert len(budget(exercise_database)["reserved"]) == len(effects)
        raise ToolRejected("候选质量未通过")
    monkeypatch.setattr(delegation, "knowledge", reject)
    async def scenario():
        for index in range(8):
            with pytest.raises(ToolRejected):
                await delegation("supervisor", "call_knowledge_agent", arguments(str(index)), context(), runtime_context=parent())
        with pytest.raises(ValueError, match="总预算"):
            await delegation("supervisor", "call_knowledge_agent", arguments("ninth"), context(), runtime_context=parent())
    asyncio.run(scenario())
    assert len(effects) == len(budget(exercise_database)["reserved"]) == 8
    with exercise_database() as db:
        assert "delegation_checkpoint" not in db.get(LearningSession, "mine").pending_context


def test_failed_operation_reuses_reservation_after_new_owner(exercise_database, monkeypatch):
    service, _ = learning_with([])
    first = start_delegation(service, exercise_database)
    async def reject(task, child, **options): raise ToolRejected("请修复候选")
    monkeypatch.setattr(first, "knowledge", reject)
    async def fail(delegation):
        with pytest.raises(ToolRejected):
            await delegation("supervisor", "call_knowledge_agent", arguments(), context(), runtime_context=parent())
    asyncio.run(fail(first))
    original = budget(exercise_database)
    service.release("mine", "budget-turn-1", failed=True)
    restored = start_delegation(service, exercise_database, turn_id="retry")
    monkeypatch.setattr(restored, "knowledge", reject)
    asyncio.run(fail(restored))
    assert budget(exercise_database) == original and len(original["reserved"]) == 1


def test_completed_receipt_replays_without_reexecuting_or_new_budget(exercise_database, monkeypatch):
    service, _ = learning_with([])
    delegation = start_delegation(service, exercise_database)
    executed = []
    async def complete(task, child, **options):
        executed.append(task)
        return {"agent": "knowledge", "child_run_ids": ["child"], "status": "completed"}
    monkeypatch.setattr(delegation, "knowledge", complete)
    async def call(value):
        return await value("supervisor", "call_knowledge_agent", arguments(), context(), runtime_context=parent())
    result = asyncio.run(call(delegation))
    service.release("mine", "budget-turn-1", failed=True)
    restored = start_delegation(service, exercise_database, turn_id="retry")
    async def forbidden(*args, **kwargs): pytest.fail("完成结果不应重新执行")
    monkeypatch.setattr(restored, "knowledge", forbidden)
    assert asyncio.run(call(restored)) == {**result, "replayed": True}
    assert len(executed) == len(budget(exercise_database)["reserved"]) == 1
    with exercise_database() as db:
        rows = [row for row in db.scalars(select(Message)) if row.metadata_json.get("_delegation_receipt")]
        assert len(rows) == 1


def test_new_request_has_independent_budget_and_stale_owner_cannot_reserve(exercise_database):
    service, _ = learning_with([])
    old = start_delegation(service, exercise_database)
    old._reserve("first")
    service.release("mine", "budget-turn-1", failed=True)
    new = start_delegation(service, exercise_database, request_id="new-request", turn_id="new-owner")
    assert budget(exercise_database, "new-request") == {}
    with pytest.raises(LearningTurnLost): old._reserve("late")
    new._reserve("new")
    assert budget(exercise_database)["reserved"] == ["first"]
    assert budget(exercise_database, "new-request")["reserved"] == ["new"]

@pytest.mark.parametrize(
    "error,expected_type",
    [
        (ToolRejected("质量未通过"), ToolRejected),
        (
            ExceptionGroup(
                "内层质量拒绝", [ToolRejected("候选一"), ToolRejected("候选二")]
            ),
            ToolRejected,
        ),
        (RuntimeError("服务不可用"), ValueError),
        (
            ExceptionGroup(
                "混合失败", [ToolRejected("候选质量"), RuntimeError("服务不可用")]
            ),
            ValueError,
        ),
    ],
)
def test_batch_quality_classification_checks_all_exception_leaves(
    exercise_database, error, expected_type
):
    assert is_execution_error(error) is (expected_type is ValueError)


@pytest.mark.parametrize("nested", [False, True])
def test_batch_ownership_loss_escapes_even_from_nested_mixed_failures(
    exercise_database, nested
):
    lost_owner = LearningTurnLost("旧批量任务已失去会话归属")
    error = (
        ExceptionGroup(
            "混合失败",
            [
                ToolRejected("候选题质量不足"),
                ExceptionGroup("内部执行失败", [RuntimeError("网络中断"), lost_owner]),
            ],
        )
        if nested
        else lost_owner
    )

    with pytest.raises(LearningTurnLost, match="失去会话归属") as caught:
        raise_ownership_loss(error)
    assert caught.value is lost_owner
