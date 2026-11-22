"""使用真实业务服务和脚本模型验证子任务返回、长期任务接续与失败恢复。"""

import asyncio
import json

import pytest
from sqlalchemy import func, select

from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.assessment.models import Attempt
from app.domains.courses.models import Concept
from app.domains.sessions.models import LearningSession, Message
from app.domains.learning.schemas import MessageRequest
from app.domains.sessions.service import SessionService
from test_exercise_context import exercise_database, pose
from test_agent_autonomy import ScriptedModel, final, learning_with, tool


def continuation(**extra):
    return {
        "status": "awaiting_student",
        "current_step": "已讲解当前例题，继续原计数学习任务",
        "next_step": "请说明什么时候应把每一步的选择数相乘",
        "completed_steps": ["例题讲解"],
        **extra,
    }


def lesson():
    return {
        "title": "计数中的限制条件",
        "content": "先明确对象和限制条件，再区分分类计数与分步计数。分类互不重叠时相加，分步选择时相乘，需要检查每个阶段的选择数是否依赖之前结果。",
        "follow_up": "能否重复选择会怎样改变结果？",
        "next_action": "等待真实回答",
    }


def delegated(name, **args):
    return tool(name, concept_ids=["math:c1"], **args)


def request(learning, key="delegation-request-1"):
    wait = learning.wait("mine")
    return MessageRequest(
        content="讲解这道题，然后回到计数学习计划继续讲解限制条件。", request_id=key,
        **({"wait_id": wait["wait_id"]} if wait else {}),
    )


def test_explanation_returns_to_same_supervisor_and_it_calls_next_agent(
    exercise_database,
):
    pose(exercise_database)
    service, model = learning_with(
        [
            delegated("call_exercise_agent", operation="explain"),
            {
                "content": "从原题条件逐步枚举，不允许重复选择时逐步减少可用对象。",
                "key_points": ["先看限制"],
            },
            delegated("call_knowledge_agent", task="接续原目标，讲清限制条件"),
            lesson(),
            final(
                "respond",
                ["math:c1"],
                message="例题已经说明限制条件的作用，接下来请你判断一个新情境。",
                continuation=continuation(),
            ),
        ]
    )
    events = []

    async def emit(event, data):
        events.append((event, data))

    result = asyncio.run(service.turn("mine", request(service), "turn-delegate", emit))
    final_context = model.calls[-1][0]
    receipts = [json.loads(m["content"]) for m in final_context if m["role"] == "tool"]
    assert [r["agent"] for r in receipts] == ["exercise", "knowledge"]
    assert result["message"]["agent"] == "supervisor"
    assert "接下来" in result["message"]["content"]
    assert (
        len([e for e, d in events if e == "message" and d["role"] == "assistant"]) == 1
    )
    restored = SessionService().get("mine")
    assert restored["goal"] == "学习计数"
    assert restored["pending_assisted"] is True
    assert restored["learning_progress"]["objective"] == "学习计数"
    assert restored["learning_progress"]["status"] == "awaiting_student"
    with exercise_database() as db:
        assert db.scalar(select(func.count()).select_from(Attempt)) == 0
        parent = db.scalar(select(AgentRun).where(AgentRun.agent == "supervisor"))
        assert [t["name"] for t in parent.tool_calls] == [
            "call_exercise_agent",
            "call_knowledge_agent",
        ]
        assert all(
            t["parent_run_id"] == parent.id and t["child_run_ids"]
            for t in parent.tool_calls
        )


def test_knowledge_waits_for_exercise_then_returns_to_supervisor(exercise_database):
    pose(exercise_database)
    service, model = learning_with(
        [
            delegated("call_knowledge_agent", task="结合这道题讲解限制条件"),
            delegated(
                "call_exercise_agent", operation="explain", task="解释本题第二问"
            ),
            {
                "content": "第二问明确禁止重复选择，所以后续位置的可选物品会减少。",
                "key_points": ["禁止重复"],
            },
            lesson(),
            final("teach", ["math:c1"], continuation=continuation()),
        ]
    )
    result = asyncio.run(service.turn("mine", request(service), "turn-nested", _emit))
    knowledge_messages = model.calls[3][0]
    returned = [
        json.loads(m["content"]) for m in knowledge_messages if m["role"] == "tool"
    ]
    assert returned[0]["agent"] == "exercise"
    assert any(
        m["role"] == "tool" and "限制条件" in m["content"] for m in model.calls[-1][0]
    )
    assert result["message"]["agent"] == "supervisor"
    assert result["lesson"]["title"] == "计数中的限制条件"


def test_parent_failure_reuses_completed_child_and_does_not_repeat_user_message(
    exercise_database,
):
    service, model = learning_with(
        [delegated("call_knowledge_agent", task="说明限制条件"), lesson()]
    )
    with pytest.raises(RuntimeError, match="StopIteration"):
        asyncio.run(service.turn("mine", request(service), "turn-failed", _emit))
    assert SessionService().get("mine")["active_turn_id"] is None
    model.responses = iter([final("respond", ["math:c1"], continuation=continuation())])
    result = asyncio.run(service.turn("mine", request(service), "turn-retry", _emit))
    assert result["lesson"]["title"] == "计数中的限制条件"
    resumed_payload = json.loads(model.calls[-1][0][1]["content"])
    assert len(resumed_payload["completed_delegations"]) == 1
    with exercise_database() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.agent == "knowledge")
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count()).select_from(Message).where(Message.role == "user")
            )
            == 1
        )
    before = len(model.calls)
    replay = asyncio.run(service.turn("mine", request(service), "turn-replay", _emit))
    assert replay["replayed"] and len(model.calls) == before


def test_same_parent_delegations_share_budget_and_every_call_has_receipt(
    exercise_database,
):
    calls = []
    for i in range(9):
        value = delegated("call_knowledge_agent", task=f"限制条件子任务{i}")[
            "tool_calls"
        ][0]
        calls.append({**value, "id": f"delegate-{i}"})
    service, model = learning_with(
        [
            {"tool_calls": calls},
            *[lesson() for _ in range(8)],
            final("respond", ["math:c1"], continuation=continuation()),
        ]
    )
    asyncio.run(service.turn("mine", request(service), "turn-budget", _emit))
    receipts = [
        json.loads(m["content"]) for m in model.calls[-1][0] if m["role"] == "tool"
    ]
    assert len(receipts) == 9
    assert "总预算" in receipts[-1]["message"]
    with exercise_database() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.agent == "knowledge")
            )
            == 8
        )


def test_cannot_finish_task_while_student_has_pending_question(exercise_database):
    pose(exercise_database)
    service, model = learning_with(
        [
            delegated("call_knowledge_agent", task="讲清条件"),
            lesson(),
            final(
                "respond", ["math:c1"], continuation=continuation(status="completed")
            ),
            final("respond", ["math:c1"], continuation=continuation()),
        ]
    )
    asyncio.run(service.turn("mine", request(service), "turn-stop", _emit))
    assert any(
        "不能标为完成" in m["content"]
        for m in model.calls[-1][0]
        if m["role"] == "user"
    )
    assert SessionService().get("mine")["phase"] == "awaiting_answer"


async def _emit(*args):
    pass


def test_parent_provided_example_can_use_different_original_concept_tags(
    exercise_database,
):
    pose(exercise_database)
    with exercise_database() as db:
        db.add(
            Concept(
                id="math:c2",
                course_id="math",
                source_id="c2",
                name="计数的一般方法",
                topic_type="concept",
            )
        )
    service, model = learning_with(
        [
            tool(
                "call_knowledge_agent",
                task="结合父级提供的例题讲一般计数方法",
                concept_ids=["math:c2"],
            ),
            delegated("call_exercise_agent", operation="explain"),
            {
                "content": "按题干限制逐步计数，每一步选择均须满足不能重复的条件。",
                "key_points": ["逐步计数"],
            },
            lesson(),
            final("teach", ["math:c2"], continuation=continuation()),
        ]
    )
    result = asyncio.run(service.turn("mine", request(service), "turn-example-tags", _emit))
    receipts = [
        json.loads(m["content"]) for m in model.calls[3][0] if m["role"] == "tool"
    ]
    assert receipts[0]["agent"] == "exercise" and "error" not in receipts[0]
    assert result["message"]["agent"] == "supervisor"
    assert SessionService().get("mine")["pending_question"]["concept_ids"] == [
        "math:c1"
    ]


@pytest.mark.parametrize("quote", [None, "用户并未说过的更换目标要求"])
def test_stage_progress_cannot_silently_rewrite_the_long_term_goal(
    exercise_database, quote
):
    service, model = learning_with(
        [
            final("respond", goal_update="只完成一个子步骤", goal_update_request=quote),
            final("respond", message="继续原目标，当前先理解限制条件。"),
        ]
    )
    asyncio.run(service.turn("mine", request(service), "turn-goal-anchor", _emit))
    assert SessionService().get("mine")["goal"] == "学习计数"
    assert any(
        "更换整体学习目标" in m["content"]
        for m in model.calls[-1][0]
        if m["role"] == "user"
    )


def test_example_permission_is_bound_to_frozen_record_not_just_same_concept(
    exercise_database,
):
    from uuid import uuid4

    old_id, current_id = str(uuid4()), str(uuid4())
    pose(exercise_database, eid=old_id)
    pose(exercise_database, qid="math:q2", eid=current_id, second=2)
    service, model = learning_with(
        [
            delegated("call_knowledge_agent", task="只围绕当前例题讲方法"),
            delegated("call_exercise_agent", operation="explain", exercise_id=old_id),
            lesson(),
            final("teach", ["math:c1"], continuation=continuation()),
        ]
    )
    asyncio.run(service.turn("mine", request(service), "turn-example-capability", _emit))
    receipts = [
        json.loads(m["content"]) for m in model.calls[2][0] if m["role"] == "tool"
    ]
    assert receipts[0]["error"] == "PermissionError"
    assert "父级提供" in receipts[0]["message"]
    with exercise_database() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.agent == "exercise")
            )
            == 0
        )
    assert SessionService().get("mine")["pending_question"]["id"] == "math:q2"
