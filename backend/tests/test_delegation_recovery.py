"""在隔离业务库验证嵌套委派恢复、冻结历史和旧轮次写入隔离。"""

import asyncio
from dataclasses import replace
import json
from itertools import repeat
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.courses.models import Concept, Question
from app.domains.sessions.models import LearningSession, Message
from app.agent.runtime.contracts import RuntimeContext
from app.domains.learning.schemas import MessageRequest
from app.domains.practice.batch_state import public_batch
from app.domains.courses.catalog import question_contract
from app.application.learning.delegation import LearningDelegation
from app.domains.practice.context import SessionExerciseService
from app.domains.sessions.progress import preserve_learning_context
from app.shared.execution import LearningTurnLost
from app.domains.sessions.service import SessionService
from app.agent.tools.contracts import ToolContext
from test_agent_autonomy import ScriptedModel, final, learning_with, tool
from test_exercise_context import exercise_database


async def emit(*args):
    pass


def continuation():
    return {
        "status": "awaiting_student",
        "current_step": "计数概念已讲解，巩固练习已准备",
        "next_step": "请独立回答当前练习，再按真实反馈继续学习",
        "completed_steps": ["计数讲解"],
    }


def lesson(**extra):
    return {
        "title": "区分分步与分类",
        "content": "先明确选择对象和限制条件，再区分分类计数与分步计数。分类情况互不重叠时相加，分步选择时相乘，并检查每个阶段的可选数量是否依赖此前结果。",
        "follow_up": "若允许重复选择，第二步的选择数量会怎样变化？",
        "next_action": "请完成当前独立练习",
        **extra,
    }


def knowledge_call():
    return tool(
        "call_knowledge_agent",
        task="讲解计数限制并安排巩固练习",
        concept_ids=["math:c1"],
    )


def test_nested_single_question_keeps_frozen_history_and_all_child_runs(
    exercise_database,
):
    service, model = learning_with(
        [
            knowledge_call(),
            tool(
                "call_exercise_agent",
                operation="select",
                task="选择一题巩固计数",
                concept_ids=["math:c1"],
            ),
            {"question_id": "math:q2", "reason": "该题直接考查分类选择"},
            {
                "predicted_correct": True,
                "rationale": "尚无独立作答证据，仅进行作答前预测",
            },
            lesson(),
            final("teach", ["math:c1"], continuation=continuation()),
        ]
    )

    async def retrieve(*args, **kwargs):
        return {"items": [{"question": service.catalog.question("math:q2", "math")}]}

    service.runner.tools.retrieval.retrieve = retrieve
    request = MessageRequest(
        content="先讲解计数，再选一道题练习", request_id="nested-single-recovery"
    )
    result = asyncio.run(service.turn("mine", request, "nested-select-turn", emit))
    assert result["message"]["metadata"]["kind"] == "exercise_delivery"
    assert result["question"]["id"] == "math:q2"
    assert "PRIVATE_SOLUTION" not in json.dumps(result, ensure_ascii=False)
    with exercise_database() as db:
        history = SessionExerciseService(db, "mine", "math", "local")
        records = history.list()["items"]
        assert len(records) == 1
        record = db.get(Message, records[0]["exercise_id"])
        assert record.metadata_json["kind"] == "question"
        assert record.metadata_json["record_only"] is True
        assert history.internal_contract(record.id)["answer"] == ["A"]
        assert history.internal_contract(record.id)["explanation"] == "PRIVATE_SOLUTION"
        exercise_id = record.id
        runs = {row.agent: row for row in db.scalars(select(AgentRun))}
        children = runs["knowledge"].tool_calls[0]["child_run_ids"]
        assert set(children) == {runs["exercise"].id}
        assert "assessment" not in runs  # 呈题不再触发作答前预测。
        # 原题库更新不能改变已经冻结的本次练习及其参考答案。
        current = db.get(Question, "math:q2")
        current.version += 1
        current.stem = "此处是后续更新过的题干"
        current.answer = ["B"]
        current.explanation = "UPDATED_PRIVATE_SOLUTION"
    with exercise_database() as db:
        frozen = SessionExerciseService(db, "mine", "math", "local").internal_contract(
            exercise_id
        )
        assert frozen["stem"] == result["question"]["stem"]
        assert frozen["answer"] == ["A"] and frozen["explanation"] == "PRIVATE_SOLUTION"
    supervisor_messages = model.calls[-1][0]
    assert any(
        m["role"] == "tool" and '"agent": "knowledge"' in m["content"]
        for m in supervisor_messages
    )


def test_retry_consumes_checkpointed_nested_batch_without_regeneration_or_assistance(
    exercise_database, monkeypatch
):
    spec = {
        "title": "计数独立练习",
        "tasks": [
            {
                "concept_ids": ["math:c1"],
                "cognitive_type": "understanding",
                "question_type": "choice",
            }
        ],
    }
    batch_call = tool(
        "call_exercise_agent",
        operation="batch",
        task="按讲解准备练习",
        concept_ids=["math:c1"],
        exercise_batch=spec,
    )
    service, model = learning_with([knowledge_call(), batch_call])
    generated = []

    async def generate(specification, context, turn_id, sink, *, teaching_request="", **options):
        assert teaching_request == "按讲解准备练习"
        generated.append(specification)
        batch_id, exercise_id = str(uuid4()), str(uuid4())
        with exercise_database() as db:
            contract = question_contract(db.get(Question, "math:q2"))
            batch = {
                "id": batch_id,
                "title": specification["title"],
                "items": [
                    {
                        "contract": contract,
                        "cognitive_type": "understanding",
                        "target_concept_ids": ["math:c1"],
                        "assisted": False,
                        "exercise_id": exercise_id,
                    }
                ],
            }
            row = db.get(LearningSession, context.session_id)
            assert row.active_turn_id == turn_id
            row.pending_question_id = contract["id"]
            row.pending_context = {
                **preserve_learning_context(row.pending_context),
                "exercise_batch": batch,
            }
            row.phase = "awaiting_answer"
            public = public_batch(batch, context.course_id)
            db.add(
                Message(
                    id=exercise_id,
                    session_id=context.session_id,
                    role="assistant",
                    agent="exercise",
                    content="请独立作答。",
                    metadata_json={
                        "kind": "question",
                        "batch_id": batch_id,
                        "question_id": contract["id"],
                        "question": public["questions"][0],
                        "_exercise_contract": contract,
                    },
                )
            )
        await sink("exercise_batch", public)
        return {"exercise_batch": public}, []

    monkeypatch.setattr(service.batches, "generate", generate)
    request = MessageRequest(
        content="讲解后安排一道计数练习",
        request_id="nested-batch-checkpoint",
        exercise_count=1,
    )
    with pytest.raises(RuntimeError, match="StopIteration"):
        asyncio.run(service.turn("mine", request, "nested-batch-failed", emit))
    with exercise_database() as db:
        row = db.get(LearningSession, "mine")
        batch_id = row.pending_context["exercise_batch"]["id"]
        outcomes = [message.metadata_json["_delegation_receipt"]["outcome"] for message in db.scalars(select(Message)) if message.metadata_json.get("_delegation_receipt")]
        assert [outcome["name"] for outcome in outcomes] == [
            "call_exercise_agent"
        ]
        assert not row.pending_context["exercise_batch"]["items"][0]["assisted"]
        assert row.active_turn_id is None
    model.responses = iter(
        [
            knowledge_call(),
            batch_call,
            lesson(exercise_batch=spec),
            final("teach", ["math:c1"], continuation=continuation()),
        ]
    )
    result = asyncio.run(service.turn("mine", request, "nested-batch-retry", emit))
    assert len(generated) == 1
    assert result["exercise_batch"]["id"] == batch_id
    assert result["lesson"]["exercise_batch"]["tasks"][0]["concept_ids"] == ["math:c1"]
    with exercise_database() as db:
        row = db.get(LearningSession, "mine")
        assert not row.pending_context["exercise_batch"]["items"][0]["assisted"]
        assert (
            db.scalar(
                select(func.count()).select_from(Message).where(Message.role == "user")
            )
            == 1
        )
        assert len([message for message in db.scalars(select(Message)) if message.metadata_json.get("_delegation_receipt")]) == 2
    knowledge_responses = [
        messages
        for messages, _ in model.calls
        if "teaching_request" in json.loads(messages[1]["content"])
    ]
    returned = [
        json.loads(m["content"]) for m in knowledge_responses[-1] if m["role"] == "tool" and m["content"].startswith("{")
    ]
    assert returned[-1]["replayed"] is True and "error" not in returned[-1]


def test_stale_parent_cannot_commit_plan_progress_or_final_message(exercise_database):
    service, _ = learning_with([])

    class AwaitedModel(ScriptedModel):
        def __init__(self):
            super().__init__(
                repeat(
                    final(
                        "respond",
                        goal_update="旧任务不应覆盖新目标",
                        continuation=continuation(),
                    )
                )
            )
            self.started, self.release = asyncio.Event(), asyncio.Event()

        async def chat(self, messages, **kwargs):
            self.started.set()
            await self.release.wait()
            return await super().chat(messages, **kwargs)

    async def scenario():
        model = AwaitedModel()
        service.runner.models = model
        request = MessageRequest(
            content="请继续旧任务", request_id="stale-parent-request"
        )
        task = asyncio.create_task(service.turn("mine", request, "old-turn", emit))
        await asyncio.wait_for(model.started.wait(), 1)
        with exercise_database() as db:
            row = db.get(LearningSession, "mine")
            row.active_turn_id = "new-turn"
            row.goal = "新任务目标"
            row.pending_context = {
                "learning_progress": {
                    "objective": "新任务目标",
                    "current_step": "新轮次正在执行",
                }
            }
        model.release.set()
        with pytest.raises(LearningTurnLost):
            await task
        assert len(model.calls) == 1

    asyncio.run(scenario())
    with exercise_database() as db:
        row = db.get(LearningSession, "mine")
        assert row.active_turn_id == "new-turn"
        assert row.goal == "新任务目标"
        assert (
            row.pending_context["learning_progress"]["current_step"] == "新轮次正在执行"
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.role == "assistant")
            )
            == 0
        )


@pytest.mark.parametrize("violation", ["course", "student", "target"])
def test_nested_exercise_cannot_escape_course_student_or_teaching_scope(
    exercise_database, violation
):
    service, model = learning_with([])
    with exercise_database() as db:
        db.add(
            Concept(id="math:c2", course_id="math", source_id="c2", name="其他知识点")
        )
    snapshot = service.sessions.get("mine")
    state = service.claim("mine", "scope-turn")
    request = MessageRequest(content="讲解当前范围", request_id="nested-scope-request")
    delegation = LearningDelegation(
        service, state, snapshot, request, "scope-turn", emit, exercise_database
    )
    context = ToolContext(
        course_id="math", session_id="mine", student_id="local", concept_ids=["math:c1"]
    )
    args = {"operation": "select", "concept_ids": ["math:c1"]}
    if violation == "course":
        context = replace(context, course_id="mechanics")
    elif violation == "student":
        context = replace(context, student_id="someone")
    else:
        args["concept_ids"] = ["math:c2"]

    async def execute():
        runtime = RuntimeContext(scope=context, role="knowledge", run_id="scope-knowledge", root_run_id="scope-parent", parent_run_id="scope-parent", depth=1)
        with pytest.raises(PermissionError):
            await delegation("knowledge", "call_exercise_agent", args, context, runtime_context=runtime)

    try:
        asyncio.run(execute())
        assert model.calls == [] and delegation.outcomes == []
    finally:
        service.release("mine", "scope-turn")
    with exercise_database() as db:
        assert db.get(LearningSession, "mine").pending_question_id is None
