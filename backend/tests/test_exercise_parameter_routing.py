"""验证真实委派到生成器的要求转发，不调用远程模型或数学求解器。"""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.sessions.models import LearningSession
from app.domains.learning.schemas import MessageRequest
from app.application.learning.delegation import LearningDelegation
from app.domains.sessions.service import SessionService
from app.agent.tools.contracts import ToolContext
from test_agent_autonomy import final, learning_with, tool
from test_exercise_context import exercise_database


async def emit(*args):
    pass


@pytest.fixture
def routing_database(exercise_database, monkeypatch):
    monkeypatch.setattr("app.domains.practice.batches.db_session", exercise_database)
    return exercise_database


def plan(question_type="fill", *, cognitive_type="application", count=1):
    return {
        "title": "按要求生成练习",
        "tasks": [
            {
                "concept_ids": ["math:c1"],
                "cognitive_type": cognitive_type,
                "question_type": question_type,
                "difficulty_range": {"minimum": 65, "maximum": 90},
                "instruction": f"第{index + 1}题围绕限制条件建模，提供必要数据。",
            }
            for index in range(count)
        ],
    }


def continuation():
    return {
        "status": "awaiting_student",
        "current_step": "练习已准备，等待真实作答",
        "next_step": "依据真实回答继续讲解计数",
        "completed_steps": ["准备练习"],
    }


def install_generator(service, monkeypatch):
    captured = []

    async def generate(context, turn_id, cognitive_type, sink, *, assignment, operation_id,
                       config=None, task_id="single", runtime_context=None):
        captured.append(
            {
                "concept_ids": list(context.concept_ids),
                "cognitive_type": cognitive_type,
                "assignment": deepcopy(assignment),
            }
        )
        # 固定返回可发布的题目契约，将测试范围限定为路由和冻结流程。
        contract = service.catalog.question("math:q2", "math", internal=True)
        kind = assignment["question_type"]
        contract.update(
            {
                "id": f"math:generated:routing-{assignment['index']}",
                "stem": f"第{assignment['index']}题：从四个不同物品中选择一个，共有多少种选择？",
                "question_type": kind,
                "options": (
                    {} if kind == "fill" else {"A": "4", "B": "3", "C": "2", "D": "1"}
                ),
                "answer": ["4"] if kind == "fill" else ["A"],
                "concept_ids": list(context.concept_ids),
                "cognitive_type": cognitive_type,
                "generated": True,
            }
        )
        return contract, {"run_id": str(uuid4()), "citations": []}

    monkeypatch.setattr(service.exercises, "generate", generate)
    return captured


def test_single_generate_transfers_the_complete_validated_fill_task(
    routing_database, monkeypatch
):
    specification = plan()
    teaching_request = "原创较难的和差倍应用题，不能改成单选题。" * 60
    service, _ = learning_with(
        [
            tool(
                "call_exercise_agent",
                operation="generate",
                task=teaching_request,
                concept_ids=["math:c1"],
                exercise_batch=specification,
            ),
            final("practice", ["math:c1"], continuation=continuation()),
        ]
    )
    captured = install_generator(service, monkeypatch)
    result = asyncio.run(
        service.turn(
            "mine",
            MessageRequest(
                content="请出一道较难单空填空题",
                request_id="routing-fill-1",
                cognitive_type="application",
                exercise_count=1,
            ),
            "routing-fill-turn",
            emit,
        )
    )
    assignment = captured[0]["assignment"]
    assert captured[0]["cognitive_type"] == "application"
    assert assignment["question_type"] == "fill"
    assert assignment["difficulty_range"] == {"minimum": 65, "maximum": 90}
    assert assignment["instruction"] == specification["tasks"][0]["instruction"]
    assert assignment["teaching_request"] == teaching_request
    assert result["question"]["question_type"] == "fill"
    assert SessionService().get("mine")["pending_question"]["question_type"] == "fill"


@pytest.mark.parametrize("teaching_request", ["原创计数应用题，使用体育活动情境。", ""])
def test_single_generate_without_plan_preserves_default_and_teaching_request(
    routing_database, monkeypatch, teaching_request
):
    user_message = "请给我出一道计数应用题"
    service, _ = learning_with(
        [
            tool(
                "call_exercise_agent",
                operation="generate",
                task=teaching_request,
                concept_ids=["math:c1"],
                cognitive_type="application",
            ),
            final("practice", ["math:c1"], continuation=continuation()),
        ]
    )
    captured = install_generator(service, monkeypatch)
    result = asyncio.run(
        service.turn(
            "mine",
            MessageRequest(content=user_message, request_id="routing-default-1"),
            "routing-default-turn",
            emit,
        )
    )
    assignment = captured[0]["assignment"]
    assert assignment["teaching_request"] == (teaching_request or user_message)
    assert assignment["question_type"] == "choice"
    assert assignment["difficulty_range"] is None
    assert result["question"]["question_type"] == "choice"


def test_batch_transfers_full_parent_request_and_each_task_requirements(
    routing_database, monkeypatch
):
    specification = plan(count=2)
    specification["tasks"][1].update(question_type="choice", cognitive_type="recall")
    teaching_request = "围绕指定考点原创，采用不同情境，保留每题的题型和认知目标。" * 50
    service, _ = learning_with(
        [
            tool(
                "call_exercise_agent",
                operation="batch",
                task=teaching_request,
                concept_ids=["math:c1"],
                exercise_batch=specification,
            ),
            final(
                "practice",
                ["math:c1"],
                exercise_batch=specification,
                continuation=continuation(),
            ),
        ]
    )
    captured = install_generator(service, monkeypatch)
    result = asyncio.run(
        service.turn(
            "mine",
            MessageRequest(
                content="请准备两道计数练习",
                request_id="routing-batch-1",
                exercise_count=2,
            ),
            "routing-batch-turn",
            emit,
        )
    )
    assert len(result["exercise_batch"]["questions"]) == len(captured) == 2
    for item in captured:
        assignment = item["assignment"]
        expected = specification["tasks"][assignment["index"] - 1]
        assert assignment["teaching_request"] == teaching_request
        assert assignment["instruction"] == expected["instruction"]
        assert assignment["question_type"] == expected["question_type"]
        assert assignment["difficulty_range"] == expected["difficulty_range"]
        assert item["cognitive_type"] == expected["cognitive_type"]
        assert assignment["total"] == 2


@pytest.mark.parametrize(
    "operation,count",
    [
        ("generate", 2),
        ("batch", None),
        ("select", 1),
        ("explain", 1),
        ("unknown", None),
    ],
)
def test_direct_service_call_cannot_bypass_operation_contract(
    routing_database, operation, count
):
    service, model = learning_with([])
    snapshot = service.sessions.get("mine")
    state = service.claim("mine", "routing-rejected-turn")
    request = MessageRequest(content="按计划准备练习", request_id="routing-rejected-1")
    delegation = LearningDelegation(
        service,
        state,
        snapshot,
        request,
        "routing-rejected-turn",
        emit,
        routing_database,
    )
    arguments = {"operation": operation, "concept_ids": ["math:c1"]}
    if count is not None:
        arguments["exercise_batch"] = plan(count=count)
    with pytest.raises(ValueError):
        asyncio.run(
            delegation.exercise(
                arguments,
                ToolContext(
                    course_id="math",
                    session_id="mine",
                    student_id="local",
                    concept_ids=["math:c1"],
                ),
            )
        )
    assert model.calls == []
    with routing_database() as db:
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 0
        assert db.get(LearningSession, "mine").pending_question_id is None
