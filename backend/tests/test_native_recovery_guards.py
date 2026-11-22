"""故障窗口中的帮助条件写入与未发布生成候选撤销。"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import TypedDict

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from app.agent.tools.contracts import ToolContext
from app.application.learning.delegation import LearningDelegation
from app.application.learning.service import LearningService
from app.application.learning.subgraphs.practice import generation_graph
from app.domains.courses.models import Question
from app.domains.learning.schemas import MessageRequest
from app.domains.memory.scopes.dependencies import ensure_scope_state
from app.domains.practice.service import PracticeService
from app.domains.sessions.models import LearningSession, Message
from app.shared.execution import LearningConflict, LearningTurnLost
from test_exercise_context import exercise_database, pose
from test_native_practice_graph import practice_database, services, context, emit


def delegation_for(database):
    state = {"course_id": "math", "student_id": "local", "goal": "学习计数", "memory_epoch": 0}
    scope = ToolContext(course_id="math", student_id="local", session_id="mine",
                        concept_ids=["math:c1"], request_id="assistance-guard", memory_epoch=0)
    delegate = LearningDelegation(SimpleNamespace(), state,
        {"id": "mine", "agenda": [], "messages": []},
        MessageRequest(content="解释当前题。", request_id=scope.request_id), "owner", emit, database)
    return delegate, scope


async def call_assistance(delegate, scope, operation):
    if operation == "knowledge":
        return await delegate.knowledge({"task": "说明限制条件"}, scope)
    return await delegate.exercise({"operation": "explain", "task": "说明限制条件", "exercise_id": "exercise-1"},
                                   scope, runtime_context=SimpleNamespace(role="supervisor"))


@pytest.mark.parametrize("operation", ["knowledge", "explain"])
@pytest.mark.parametrize("replacement", [None, "new-owner"])
def test_lost_owner_cannot_modify_current_assistance(exercise_database, monkeypatch, operation, replacement):
    pose(exercise_database)
    with exercise_database() as db:
        db.get(LearningSession, "mine").active_turn_id = "owner"
    delegate, scope = delegation_for(exercise_database)
    checked = delegate.session()
    # 模拟已完成前置检查、即将进入帮助条件写事务时执行权被撤销。
    monkeypatch.setattr(delegate, "session", lambda: checked)
    with exercise_database() as db:
        row = db.get(LearningSession, "mine")
        row.active_turn_id = replacement
        original = deepcopy(row.pending_context)
    with pytest.raises(LearningTurnLost):
        asyncio.run(call_assistance(delegate, scope, operation))
    with exercise_database() as db:
        assert db.get(LearningSession, "mine").pending_context == original


@pytest.mark.parametrize("operation", ["knowledge", "explain"])
def test_revoked_memory_cannot_modify_assistance(exercise_database, operation):
    pose(exercise_database)
    with exercise_database() as db:
        row = db.get(LearningSession, "mine")
        row.active_turn_id = "owner"
        original = deepcopy(row.pending_context)
    delegate, scope = delegation_for(exercise_database)
    with exercise_database() as db:
        ensure_scope_state(db, "local", "student").invalidation_version += 1
    with pytest.raises(LearningConflict, match="撤销"):
        asyncio.run(call_assistance(delegate, scope, operation))
    with exercise_database() as db:
        assert db.get(LearningSession, "mine").pending_context == original


def test_knowledge_cannot_mark_replaced_pending_question(exercise_database, monkeypatch):
    pose(exercise_database)
    with exercise_database() as db:
        db.get(LearningSession, "mine").active_turn_id = "owner"
    delegate, scope = delegation_for(exercise_database)
    checked = delegate.session()
    monkeypatch.setattr(delegate, "session", lambda: checked)
    pose(exercise_database, "math:q2", eid="exercise-2", second=2)
    with exercise_database() as db:
        row = db.get(LearningSession, "mine")
        row.version += 1
        original = deepcopy(row.pending_context)
    with pytest.raises(LearningTurnLost, match="待答题"):
        asyncio.run(call_assistance(delegate, scope, "knowledge"))
    with exercise_database() as db:
        assert db.get(LearningSession, "mine").pending_context == original


@pytest.mark.asyncio
async def test_accepted_unpublished_question_cannot_cross_revocation_and_lost_checkpoint(practice_database, tmp_path):
    exercises, _, runner = services(practice_database)
    scope = replace(context("revoked-unpublished"), memory_epoch=0)
    path = str(tmp_path / "unpublished.sqlite")
    config = {"configurable": {"thread_id": "unpublished-candidate"}, "metadata": {"tutor_durable": True}}

    class ParentState(TypedDict, total=False):
        result: dict

    def parent(saver, current_exercises, current_scope, sink):
        async def work(state, config):
            contract, result = await current_exercises.generate(current_scope, "owner", "understanding", sink,
                assignment={"question_type": "fill"}, config=config)
            return {"result": {"contract": contract, "receipt": result}}

        graph = StateGraph(ParentState)
        graph.add_node("work", work)
        graph.add_edge(START, "work")
        graph.add_edge("work", END)
        return graph.compile(checkpointer=saver)

    async def fail_after_acceptance(event, data):
        if event == "exercise_quality_checked" and data.get("status") == "accepted":
            raise RuntimeError("题目已入库，尚未冻结发布时故障")

    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = parent(saver, exercises, scope, fail_after_acceptance)
        with pytest.raises(RuntimeError, match="尚未冻结"):
            await graph.ainvoke({}, config, durability="sync")
        with practice_database() as db:
            generated = list(db.scalars(select(Question)))
            assert len(generated) == 1 and not list(db.scalars(select(Message)))
            saved_id, saved_validation = generated[0].id, deepcopy(generated[0].validation)
            assert saved_validation["generation_operation"]["memory_epoch"] == 0
            ensure_scope_state(db, "student", "student").invalidation_version += 1
        # 唯一入口会淘汰失效线程；这里明确模拟同一检查点丢失后的安全重建。
        await saver.adelete_thread(config["configurable"]["thread_id"])
    calls = len(runner.calls)
    restored = PracticeService(runner, generation_graph=generation_graph())
    current_scope = replace(scope, memory_epoch=1)
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = parent(saver, restored, current_scope, emit)
        with pytest.raises(LearningConflict, match="新请求"):
            await graph.ainvoke({}, config, durability="sync")
    assert len(runner.calls) == calls == 3
    with practice_database() as db:
        assert db.get(Question, saved_id).validation == saved_validation
        assert len(list(db.scalars(select(Question)))) == 1
        assert not list(db.scalars(select(Message)))
        assert db.get(LearningSession, "session").pending_question_id is None
    # 新请求具有新候选身份；旧产物和失败请求均不被覆盖或重新解释。
    contract, _ = await restored.generate(replace(current_scope, request_id="fresh-after-revocation"),
        "owner", "understanding", emit, assignment={"question_type": "fill"})
    assert contract["id"] != saved_id
    with practice_database() as db:
        assert db.get(Question, contract["id"]).validation["generation_operation"]["memory_epoch"] == 1
        assert len(list(db.scalars(select(Question)))) == 2


@pytest.mark.asyncio
async def test_formally_published_question_replays_after_revocation_without_generation(practice_database, monkeypatch):
    exercises, _, runner = services(practice_database)
    monkeypatch.setattr("app.application.learning.service.db_session", practice_database)
    scope = replace(context("published-before-revocation"), memory_epoch=0)
    contract, _ = await exercises.generate(scope, "owner", "understanding", emit,
                                          assignment={"question_type": "fill"}, operation_id="published-effect")
    learning = LearningService(runner)
    published = await learning.pose_contract("session", contract, scope, "owner", "understanding", emit,
                                              operation_id="published-effect")
    with practice_database() as db:
        ensure_scope_state(db, "student", "student").invalidation_version += 1
        original = deepcopy(db.get(LearningSession, "session").pending_context)
    delegate = LearningDelegation(learning,
        {"course_id": "math", "student_id": "student", "goal": "计数", "memory_epoch": 1},
        {"id": "session", "agenda": [], "messages": []},
        MessageRequest(content="继续原题。", request_id=scope.request_id), "owner", emit, practice_database)
    calls = len(runner.calls)
    result = await delegate.exercise({"operation": "generate"}, replace(scope, memory_epoch=1),
        runtime_context=SimpleNamespace(role="supervisor"), operation_id="published-effect")
    assert result["replayed"] and result["exercise_id"] == published["exercise_id"]
    assert len(runner.calls) == calls
    with practice_database() as db:
        assert db.get(LearningSession, "session").pending_context == original
        assert len(list(db.scalars(select(Question)))) == len(list(db.scalars(select(Message)))) == 1
