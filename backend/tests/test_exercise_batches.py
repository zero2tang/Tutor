"""真实服务与运行循环的隔离集成测试，不调用外部模型、检索或学生数据库。"""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select

from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.assessment.models import Attempt, BatchSubmission
from app.domains.sessions.models import LearningSession, Message
from app.domains.students.models import StudentState
from app.application.agent.runner import AgentRunner
from app.domains.assessment.schemas import AnswerRequest, BatchAnswerRequest
from app.domains.practice.schemas import ExerciseBatchRequest
from app.domains.learning.schemas import MessageRequest
from app.domains.practice.context import SessionExerciseService
from app.application.learning.service import LearningService
from app.domains.sessions.service import SessionService
from app.agent.tools.contracts import ToolContext
from app.application.agent.tools import TeachingTools
from test_exercise_context import exercise_database, assert_public, decision, emit, call


@pytest.fixture
def batch_database(exercise_database, monkeypatch):
    for module in ("app.domains.practice.batches", "app.domains.assessment.batch"):
        monkeypatch.setattr(module + ".db_session", exercise_database)
    return exercise_database


def specification(count=3):
    return {
        "title": "计数巩固",
        "tasks": [
            {
                "concept_ids": ["math:c1"],
                "cognitive_type": ["recall", "understanding", "application"][i % 3],
                "question_type": ["choice", "multi_choice", "fill"][i % 3],
                "difficulty_range": {"minimum": 10 + i * 10, "maximum": 30 + i * 10},
                "instruction": f"第{i + 1}题的考查要求",
            }
            for i in range(count)
        ],
    }


def diagnosis(**extra):
    return {
        "result": "incorrect",
        "score": 0.0,
        "confidence": 0.95,
        "feedback": "结合条件逐步检查。",
        "misconception": "",
        "prerequisite_hypothesis": "",
        "level": "beginner",
        "suggested_delta": 0.1,
        "reflection": "依据本次真实回答。",
        "next_action": "continue",
        **extra,
    }


class BatchModel:
    def __init__(self, *, source="supervisor", spec=None):
        self.settings = SimpleNamespace(llm_model="isolated-batch-test")
        self.spec = spec if spec is not None else specification()
        self.source = source
        self.calls, self.indexed = [], []
        self.messages = []
        self.inflight, self.peak = 0, 0
        self.generation_ready = None

    async def embed(self, texts):
        return [[0.1] * 8 for _ in texts]

    async def bulk_index(self, index, documents):
        self.indexed.extend(deepcopy(documents))

    async def chat(self, messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        self.calls.append(deepcopy(payload))
        self.messages.append(deepcopy(messages))
        receipts = [
            json.loads(message["content"])
            for message in kwargs.get("native_current_tool_messages", messages)
            if message["role"] == "tool"
        ]
        if "allowed_source_refs" in payload:
            output = {"topic_segments": ["已完成的整组练习编排"], "confirmed_findings": [],
                "uncertainties": ["最终学习状态以正式作答回执为准"], "unresolved_questions": [],
                "source_refs": payload["allowed_source_refs"]}
        elif payload.get("assessment_result"):
            output = decision(
                "respond", message="已经收到整组评估，将根据真实错因继续巩固。"
            )
        elif "user_message" in payload:
            if not receipts:
                output = (
                    call(
                        "call_exercise_agent",
                        operation="batch",
                        task="安排整组计数练习",
                        concept_ids=["math:c1"],
                        exercise_batch=self.spec,
                    )
                    if self.source == "supervisor"
                    else call(
                        "call_knowledge_agent",
                        task="先讲解计数条件，再安排整组练习",
                        concept_ids=["math:c1"],
                    )
                )
            elif receipts[-1].get("error"):
                output = decision(
                    "respond",
                    message="练习组未全部生成成功：" + receipts[-1]["message"],
                )
            else:
                returned = receipts[-1]
                assert returned["agent"] == (
                    "exercise" if self.source == "supervisor" else "knowledge"
                )
                if self.source == "supervisor":
                    assert len(returned["exercise_batch"]["questions"]) == len(
                        self.spec["tasks"]
                    )
                    assert_public(returned)
                else:
                    assert returned["lesson"]["exercise_batch"] == self.spec
                output = decision(
                    "practice" if self.source == "supervisor" else "teach",
                    concept_ids=["math:c1"],
                    **(
                        {"exercise_batch": self.spec}
                        if self.source == "supervisor"
                        else {}
                    ),
                )
        elif "teaching_request" in payload:
            if not receipts:
                output = call(
                    "call_exercise_agent",
                    operation="batch",
                    task="按本次讲解生成巩固题",
                    concept_ids=["math:c1"],
                    exercise_batch=self.spec,
                )
            else:
                assert receipts[-1]["agent"] == "exercise"
                assert len(receipts[-1]["exercise_batch"]["questions"]) == len(
                    self.spec["tasks"]
                )
                assert_public(receipts[-1])
                output = {
                    "title": "计数",
                    "content": "从给定物品数量出发，逐步理解计数的规则与限制条件。每次选择都需要明确是否允许重复。",
                    "follow_up": "条件有什么影响？",
                    "next_action": "练习",
                    "exercise_batch": self.spec,
                }
        elif payload.get("task") == "generate":
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            try:
                # 以事件证明首批实际重叠，不依赖5毫秒内能完成多少数据库与上下文准备。
                if self.generation_ready is None:
                    self.generation_ready = asyncio.Event()
                if self.inflight >= min(len(self.spec["tasks"]), 3):
                    self.generation_ready.set()
                await asyncio.wait_for(self.generation_ready.wait(), timeout=3)
                await asyncio.sleep(0)
                assignment = payload["assignment"]
                kind = payload["question_type"]
                output = {
                    "stem": f"第{assignment['index']}题：给定两个不同物品，选择一个，共有多少种不同选择？",
                    "question_type": kind,
                    "options": (
                        {}
                        if kind == "fill"
                        else {"A": "2", "B": "3", "C": "两个", "D": "4"}
                    ),
                    "answer": (
                        ["2"]
                        if kind == "fill"
                        else ["A", "C"] if kind == "multi_choice" else ["A"]
                    ),
                    "explanation": "PRIVATE_GENERATED_SOLUTION",
                    "concept_ids": ["math:c1"],
                    "cognitive_type": payload["cognitive_type"],
                }
            finally:
                self.inflight -= 1
        elif payload.get("task") == "validate":
            assert "answer" not in payload
            kind = payload["question_type"]
            output = {
                "answer": (
                    ["2"]
                    if kind == "fill"
                    else ["A", "C"] if kind == "multi_choice" else ["A"]
                ),
                "explanation": "独立计数得到两个。",
                "valid": True,
                "issue": "",
            }
        elif payload.get("task") == "reconcile":
            output = {
                "generation_assessment": {
                    "submitted_answer": payload["generation_solution"]["answer"],
                    "reason": "逐项核对生成答案与其计数解析一致。",
                    "correctness": "correct",
                    "answer_explanation_consistency": "consistent",
                },
                "independent_assessment": {
                    "submitted_answer": payload["independent_solution"]["answer"],
                    "reason": "逐项核对独立答案与其计数解析一致。",
                    "correctness": "correct",
                    "answer_explanation_consistency": "consistent",
                },
                "verdict": "equivalent",
                "answer": payload["generation_solution"]["answer"],
                "explanation": "COMBINED_PRIVATE_SOLUTION：两种计数表述一致，结论为两个选择。",
                "reason": "两份答案的含义一致且均正确。",
            }
        elif payload.get("task") == "batch_diagnose":
            output = {
                "items": [
                    {"question_id": item["question_id"], "diagnosis": diagnosis()}
                    for item in reversed(payload["questions"])
                ],
                "summary": "结合逐题反馈继续巩固计数与限制条件。",
                "next_action": "continue",
            }
        else:
            raise AssertionError(payload)
        return {
            "choices": [
                {
                    "message": (
                        output
                        if "tool_calls" in output
                        else {"content": json.dumps(output, ensure_ascii=False)}
                    )
                }
            ],
            "usage": {},
        }


def service(*, source="supervisor", spec=None):
    model = BatchModel(source=source, spec=spec)
    retrieval = SimpleNamespace(
        search=SimpleNamespace(QUESTIONS="fake", bulk_index=model.bulk_index)
    )
    return LearningService(AgentRunner(model, TeachingTools(retrieval)), checkpointer=InMemorySaver()), model


def generate(learning):
    return asyncio.run(
        learning.turn(
            "mine",
            MessageRequest(
                content="安排适合当前学习的练习", request_id="batch-generate-1"
            ),
            "turn-batch",
            emit,
        )
    )


def submission(batch, *, key="batch-answer-1"):
    return BatchAnswerRequest(
        batch_id=batch["id"],
        request_id=key,
        wait_id=SessionService().get("mine")["pending_interaction"]["wait_id"],
        answers=[
            {
                "question_id": question["id"],
                "answer": (
                    "2"
                    if question["question_type"] == "fill"
                    else "A,C" if question["question_type"] == "multi_choice" else "A"
                ),
            }
            for question in batch["questions"]
        ],
    )


@pytest.mark.parametrize("source", ["supervisor", "knowledge"])
def test_agents_can_request_parallel_generation_and_restore_public_batch(
    batch_database, source
):
    learning, model = service(source=source)
    result = generate(learning)
    assert "exercise_batch" in result, result
    batch = result["exercise_batch"]
    assert len(batch["questions"]) == 3 and model.peak == 3
    assert not model.indexed
    assert result["message"]["agent"] == "supervisor"
    assert result["message"]["metadata"]["delegations"]
    with batch_database() as db:
        root_run = db.scalar(select(AgentRun).where(AgentRun.agent == "supervisor"))
        assert any(
            tool["name"]
            == (
                "call_exercise_agent"
                if source == "supervisor"
                else "call_knowledge_agent"
            )
            and tool["status"] == "completed"
            and tool["child_run_ids"]
            for tool in root_run.tool_calls
        )
        if source == "knowledge":
            knowledge_run = db.scalar(
                select(AgentRun).where(AgentRun.agent == "knowledge")
            )
            assert any(
                tool["name"] == "call_exercise_agent"
                and tool["status"] == "completed"
                and tool["child_run_ids"]
                for tool in knowledge_run.tool_calls
            )
    assert not any(call.get("task") == "batch_diagnose" for call in model.calls)
    assert {q["question_type"] for q in batch["questions"]} == {
        "choice",
        "multi_choice",
        "fill",
    }
    assert batch["questions"][0]["difficulty_target"] == {
        "minimum": 10.0,
        "maximum": 30.0,
    }
    assert batch["questions"][0]["difficulty_value"] is None
    restored = learning.sessions.get("mine")
    assert restored["pending_batch"] == batch and restored["pending_question"] is None
    assert_public(restored)
    with batch_database() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.agent == "assessment")
            )
            == 0
        )
        assert db.scalar(select(func.count()).select_from(Attempt)) == 0
        context = SessionExerciseService(db, "mine", "math", "local").context()
        assert len(context["exercise_batch"]["items"]) == 3
        assert all(
            item["status"] == "awaiting_answer"
            for item in context["exercise_batch"]["items"]
        )
        assert_public(context)
        row = db.get(LearningSession, "mine")
        assert row.active_turn_id is None and row.pending_context["exercise_batch"]


@pytest.mark.parametrize("count", [1, 2, 6])
@pytest.mark.parametrize("source", ["supervisor", "knowledge"])
def test_agent_decides_one_or_many_questions_with_bounded_parallelism(
    batch_database, count, source
):
    learning, model = service(source=source, spec=specification(count))
    result = generate(learning)
    assert len(result["exercise_batch"]["questions"]) == count
    assert model.peak == min(count, 3)


def test_http_entry_generates_and_submits_a_whole_batch(batch_database):
    import httpx
    from fastapi import FastAPI
    from app.api.router import router

    learning, model = service()
    app = FastAPI()
    app.include_router(router)
    app.state.services = {"learning": learning, "sessions": learning.sessions}
    app.state.tasks = set()

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            generated = await client.post(
                "/api/sessions/mine/messages?stream=false",
                json={
                    "content": "安排适合的练习",
                    "request_id": "http-generate-1",
                },
            )
            assert generated.status_code == 200
            batch = generated.json()["exercise_batch"]
            assert_public(batch)
            submitted = submission(batch).model_dump()
            response = await client.post(
                "/api/sessions/mine/batch-answers?stream=false",
                json=submitted,
            )
            assert response.status_code == 200 and response.json()["correct_count"] == 3
            replay = await client.post(
                "/api/sessions/mine/batch-answers", json=submitted
            )
            assert replay.status_code == 200 and "event: done" in replay.text

    asyncio.run(request())
    assert sum(call.get("task") == "batch_diagnose" for call in model.calls) == 1


def test_multiple_questions_require_an_agent_authored_plan_instead_of_program_defaults(
    batch_database, monkeypatch
):
    learning, model = service()
    original = model.chat
    removed_once = False

    async def chat(messages, **kwargs):
        nonlocal removed_once
        if "user_message" in json.loads(messages[1]["content"]) and not removed_once:
            model.calls.append(deepcopy(json.loads(messages[1]["content"])))
            model.messages.append(deepcopy(messages))
            output = decision("practice", concept_ids=["math:c1"])
            removed_once = True
            return {
                "choices": [{"message": {"content": json.dumps(output)}}],
                "usage": {},
            }
        return await original(messages, **kwargs)

    monkeypatch.setattr(model, "chat", chat)
    result = asyncio.run(
        learning.turn(
            "mine",
            MessageRequest(
                content="请安排三题",
                exercise_count=3,
                request_id="explicit-count-1",
            ),
            "explicit-count-turn",
            emit,
        )
    )
    assert len(result["exercise_batch"]["questions"]) == 3
    assert sum("user_message" in call for call in model.calls) == 3
    assert "call_exercise_agent" in model.messages[1][-1]["content"]
    assert [q["question_type"] for q in result["exercise_batch"]["questions"]] == [
        "choice",
        "multi_choice",
        "fill",
    ]


def test_whole_batch_one_assessment_atomic_evidence_and_idempotent_replay(
    batch_database,
):
    learning, model = service()
    batch = generate(learning)["exercise_batch"]
    request = submission(batch)
    events = []

    async def collect(event, data):
        events.append((event, data))

    result = asyncio.run(learning.answer_batch("mine", request, "turn-answer", collect))
    assert result["total"] == result["correct_count"] == 3
    assert all(item["result"] == "correct" for item in result["items"])
    assert [item["question_id"] for item in result["items"]] == [
        q["id"] for q in batch["questions"]
    ]
    calls = [call for call in model.calls if call.get("task") == "batch_diagnose"]
    assert len(calls) == 1
    assert [item["target_level"] for item in calls[0]["questions"]] == [
        "beginner",
        "advanced",
        "proficient",
    ]
    before_replay = len(model.calls)
    replay = asyncio.run(learning.answer_batch("mine", request, "retry-turn", collect))
    assert replay["replayed"] and len(model.calls) == before_replay
    with batch_database() as db:
        assert db.scalar(select(func.count()).select_from(Attempt)) == 3
        assert db.scalar(select(func.count()).select_from(BatchSubmission)) == 1
        assert len(db.get(StudentState, "local:math:c1").evidence_questions) == 3
        session = db.get(LearningSession, "mine")
        assert not session.pending_question_id and session.active_turn_id is None
        context = SessionExerciseService(db, "mine", "math", "local").context()
        assert all(
            item["status"] == "answered" for item in context["exercise_batch"]["items"]
        )
        assert all(
            item["student_answer"] for item in context["exercise_batch"]["items"]
        )
        assert_public(context)
    assert sum(event == "batch_assessment_completed" for event, _ in events) == 1
    changed = request.model_copy(deep=True)
    changed.answers[0].answer = "B"
    with pytest.raises(ValueError, match="不同"):
        asyncio.run(learning.answer_batch("mine", changed, "retry-turn", emit))


@pytest.mark.parametrize("fault", ["missing", "duplicate", "foreign", "batch", "blank"])
def test_invalid_submissions_never_call_assessor_or_commit_partial_results(
    batch_database, fault
):
    learning, model = service()
    batch = generate(learning)["exercise_batch"]
    request = submission(batch)
    if fault == "missing":
        request.answers.pop()
    elif fault == "duplicate":
        request.answers[1].question_id = request.answers[0].question_id
    elif fault == "foreign":
        request.answers[0].question_id = "mechanics:q1"
    elif fault == "batch":
        request.batch_id = "someone-elses-batch"
    else:
        request.answers[0].answer = "   "
    before = len(model.calls)
    with pytest.raises(ValueError):
        asyncio.run(learning.answer_batch("mine", request, "invalid-turn", emit))
    assert len(model.calls) == before
    with batch_database() as db:
        assert db.scalar(select(func.count()).select_from(Attempt)) == 0
        assert db.get(LearningSession, "mine").active_turn_id is None
        assert db.get(LearningSession, "mine").pending_context["exercise_batch"]


def test_single_answer_cannot_consume_batch_and_skip_clears_whole_group(batch_database):
    learning, model = service()
    generate(learning)
    snapshot = learning.sessions.get("mine")
    wait_id = snapshot["pending_interaction"]["wait_id"]
    before = len(model.calls)
    with pytest.raises(ValueError, match="题目|整组"):
        asyncio.run(learning.answer("mine", AnswerRequest(answer="A", request_id="single-submit",
            wait_id=wait_id, question_id=snapshot["pending_batch"]["questions"][0]["id"]), "wrong-route", emit))
    assert len(model.calls) == before
    restored = asyncio.run(learning.execute("mine", {"kind": "skip", "request_id": "skip-batch",
        "wait_id": wait_id}, "skip-turn", emit))["session"]
    assert restored["pending_batch"] is None and restored["pending_question"] is None


def test_assisted_and_low_confidence_items_do_not_add_independent_evidence(
    batch_database, monkeypatch
):
    learning, model = service()
    batch = generate(learning)["exercise_batch"]
    from app.domains.practice.batch_state import mark_batch_assisted

    with batch_database() as db:
        row = db.get(LearningSession, "mine")
        row.pending_context = mark_batch_assisted(
            row.pending_context, batch["questions"][0]["id"]
        )
    original = model.chat

    async def chat(messages, **kwargs):
        response = await original(messages, **kwargs)
        payload = json.loads(messages[1]["content"])
        if payload.get("task") == "batch_diagnose":
            output = json.loads(response["choices"][0]["message"]["content"])
            for item in output["items"]:
                if item["question_id"] == batch["questions"][1]["id"]:
                    item["diagnosis"]["confidence"] = 0.5
            response["choices"][0]["message"]["content"] = json.dumps(output)
        return response

    monkeypatch.setattr(model, "chat", chat)
    result = asyncio.run(
        learning.answer_batch("mine", submission(batch), "assess", emit)
    )
    assert result["items"][0]["assisted"]
    assert (
        not result["items"][0]["state_updates"]
        and not result["items"][1]["state_updates"]
    )
    assert result["items"][2]["state_updates"]


def test_generation_failure_cancels_siblings_and_publishes_no_partial_batch(
    batch_database, monkeypatch
):
    learning, model = service()
    finished = []

    async def fail(context, turn_id, cognitive_type, emit, *, assignment, **_options):
        try:
            if assignment["index"] == 1:
                await asyncio.sleep(0)
                raise ValueError("测试生成失败")
            await asyncio.sleep(30)
        finally:
            finished.append(assignment["index"])

    monkeypatch.setattr(learning.exercises, "generate", fail)
    failed_result = generate(learning)
    assert failed_result["plan"]["action"] == "respond"
    assert "未全部生成成功" in failed_result["message"]["content"]
    assert "exercise_batch" not in failed_result
    assert set(finished) == {1, 2, 3}
    with batch_database() as db:
        row = db.get(LearningSession, "mine")
        assert row.active_turn_id is None and not row.pending_question_id
        assert not row.pending_context.get("exercise_batch")
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.metadata_json["kind"].as_string() == "question")
            )
            == 0
        )


def test_cancelled_parallel_agent_runs_do_not_remain_running(
    batch_database, monkeypatch
):
    learning, model = service()
    original = model.chat

    async def fail(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        if payload.get("task") == "generate":
            if payload["assignment"]["index"] == 1:
                await asyncio.sleep(0.005)
                raise RuntimeError("测试模型失败")
            await asyncio.Event().wait()
        return await original(messages, **kwargs)

    monkeypatch.setattr(model, "chat", fail)
    failed_result = generate(learning)
    assert failed_result["plan"]["action"] == "respond"
    assert "未全部生成成功" in failed_result["message"]["content"]
    assert "exercise_batch" not in failed_result
    with batch_database() as db:
        runs = db.scalars(select(AgentRun).where(AgentRun.agent == "exercise")).all()
        assert sorted(run.status for run in runs) == [
            "cancelled",
            "cancelled",
            "failed",
        ]


@pytest.mark.parametrize("fault", ["model", "transaction"])
def test_assessment_failure_preserves_pending_group_and_rolls_back_all_evidence(
    batch_database, monkeypatch, fault
):
    learning, model = service()
    batch = generate(learning)["exercise_batch"]
    if fault == "model":

        async def fail(*args, **kwargs):
            raise RuntimeError("测试评估模型失败")

        monkeypatch.setattr(model, "chat", fail)
    else:
        original = learning.assessment.student.apply_evidence
        called = 0

        def fail(*args, **kwargs):
            nonlocal called
            called += 1
            if called == 2:
                raise RuntimeError("测试证据写入失败")
            return original(*args, **kwargs)

        monkeypatch.setattr(learning.assessment.student, "apply_evidence", fail)
    with pytest.raises(RuntimeError):
        asyncio.run(
            learning.answer_batch("mine", submission(batch), "failed-assess", emit)
        )
    with batch_database() as db:
        assert db.scalar(select(func.count()).select_from(Attempt)) == 0
        assert db.scalar(select(func.count()).select_from(BatchSubmission)) == 0
        assert db.scalar(select(func.count()).select_from(StudentState)) == 0
        session = db.get(LearningSession, "mine")
        assert session.pending_context["exercise_batch"]["id"] == batch["id"]
        assert session.active_turn_id is None and session.phase == "awaiting_answer"


@pytest.mark.parametrize("change", ["scope", "count", "range"])
def test_request_contract_rejects_invalid_task_scope_count_and_difficulty(
    batch_database, change
):
    from app.domains.practice.batches import validate_batch_request

    spec = specification()
    if change == "scope":
        spec["tasks"][0]["concept_ids"] = ["mechanics:c1"]
    elif change == "count":
        spec["tasks"] = spec["tasks"] * 3
    else:
        spec["tasks"][0]["difficulty_range"] = {"minimum": 90, "maximum": 20}
    with pytest.raises((ValueError, ValidationError)):
        validate_batch_request(
            spec, ToolContext(course_id="math", concept_ids=["math:c1"])
        )
