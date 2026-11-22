"""用内存数据库和脚本模型验证题目追问，不访问真实学习库或模型服务。"""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.infrastructure.persistence.database import Base
from app.domains.assessment.models import Attempt
from app.domains.assessment.schemas import AnswerRequest
from app.domains.courses.models import Concept, Course, Question
from app.domains.sessions.models import LearningSession, Message
from app.application.agent.runner import AgentRunner
from app.domains.learning.schemas import MessageRequest
from app.domains.courses.catalog import question_contract
from app.domains.practice.context import SessionExerciseService
from app.application.learning.service import LearningService
from app.domains.sessions.service import SessionService
from app.agent.tools.contracts import ToolContext
from app.application.agent.tools import TeachingTools
from app.agent.runtime.contracts import RuntimeContext
from langchain.tools import ToolRuntime
from langgraph.checkpoint.memory import InMemorySaver


@pytest.fixture
def exercise_database(monkeypatch, tmp_path):
    # 文件数据库允许并发工具各自在线程内建立独立连接。
    engine = create_engine("sqlite:///" + (tmp_path / "exercises.db").as_posix())
    Base.metadata.create_all(engine)

    @contextmanager
    def database():
        with Session(engine, expire_on_commit=False) as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    for module in (
        "app.domains.courses.catalog",
        "app.application.learning.planning",
        "app.domains.sessions.service",
        "app.application.learning.service",
        "app.application.learning.entry",
        "app.application.learning.receipts",
        "app.application.learning.actions",
        "app.application.learning.publication",
        "app.domains.practice.service",
        "app.domains.assessment.service",
        "app.domains.students.service",
    ):
        monkeypatch.setattr(module + ".db_session", database, raising=False)
    monkeypatch.setattr("app.application.agent.tools.db_session", database)
    monkeypatch.setattr("app.application.agent.runner.db_session", database)
    with database() as db:
        for course_id in ("math", "mechanics"):
            db.add(
                Course(
                    id=course_id,
                    title=course_id,
                    description="测试",
                    source_ref="fixture",
                )
            )
            db.add(
                Concept(
                    id=course_id + ":c1",
                    course_id=course_id,
                    source_id="c1",
                    name="计数",
                )
            )
            for index in (1, 2):
                db.add(
                    Question(
                        id=f"{course_id}:q{index}",
                        source_id=f"q{index}",
                        course_id=course_id,
                        stem=(
                            (
                                "第一问给定三个不同物品。" * 190
                                + "第二问禁止重复选择，解释限制条件。"
                            )
                            if index == 1
                            else "从四个不同物品中选择一个。"
                        ),
                        options={"A": "3", "B": "4"},
                        answer=["A"],
                        question_type="choice",
                        concept_ids=[course_id + ":c1"],
                        explanation="PRIVATE_SOLUTION",
                        source_ref="fixture",
                        version=1,
                        validation={"private_marker": "PRIVATE_METADATA"},
                    )
                )
        for sid, course, student in (
            ("mine", "math", "local"),
            ("other", "math", "someone"),
            ("physics", "mechanics", "local"),
        ):
            db.add(
                LearningSession(
                    id=sid,
                    course_id=course,
                    student_id=student,
                    title="测试",
                    goal="学习计数",
                    concept_ids=[course + ":c1"],
                )
            )
    yield database
    engine.dispose()


def pose(
    database, qid="math:q1", *, sid="mine", eid="exercise-1", second=1, archive=True
):
    with database() as db:
        contract = question_contract(db.get(Question, qid))
        public = {
            key: value
            for key, value in contract.items()
            if key not in {"answer", "explanation"}
        }
        db.add(
            Message(
                id=eid,
                session_id=sid,
                role="assistant",
                agent="exercise",
                content="请独立作答。",
                metadata_json={
                    "kind": "question",
                    "question_id": qid,
                    "question": public,
                    **({"_exercise_contract": contract} if archive else {}),
                },
                created_at=datetime(2026, 1, 1) + timedelta(seconds=second),
            )
        )
        session = db.get(LearningSession, sid)
        session.pending_question_id = qid
        session.pending_context = {
            "contract": contract,
            "assisted": False,
            "target_concept_ids": contract["concept_ids"],
            "cognitive_type": "recall",
        }
    return contract


def decision(action="respond", **extra):
    return {
        "action": action,
        "concept_ids": [],
        "message": "根据题目条件回答。",
        "steps": [],
        "rationale": "使用会话题目快照",
        "continuation": {
            "status": (
                "awaiting_clarification" if action == "clarify" else "awaiting_student"
            ),
            "current_step": "完成当前教学或题目讨论",
            "next_step": "根据学生反馈接续学习目标。",
            "completed_steps": [],
        },
        **extra,
    }


def call(name, **arguments):
    return {
        "tool_calls": [
            {
                "id": "call-" + name,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ]
    }


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []
        self.settings = SimpleNamespace(llm_model="scripted-test")

    async def chat(self, messages, **kwargs):
        self.messages.append(deepcopy(messages))
        response = next(self.responses)
        message = (
            response
            if "tool_calls" in response
            else {"content": json.dumps(response, ensure_ascii=False)}
        )
        return {"choices": [{"message": message}], "usage": {}}


def learning(responses):
    model = Model(responses)
    registry = TeachingTools(SimpleNamespace())
    return LearningService(AgentRunner(model, registry), checkpointer=InMemorySaver()), model, registry


def current_wait():
    value = SessionService().get("mine").get("pending_interaction")
    return value["wait_id"] if value else None


async def emit(*args):
    pass


def assert_public(value):
    text = json.dumps(value, ensure_ascii=False)
    assert "PRIVATE_SOLUTION" not in text and "PRIVATE_METADATA" not in text
    if isinstance(value, dict):
        assert not {"answer", "answer_key", "explanation", "validation"} & value.keys()
        for item in value.values():
            assert_public(item)
    elif isinstance(value, list):
        for item in value:
            assert_public(item)


def test_planner_receives_full_question_outside_recent_chat(exercise_database):
    contract = pose(exercise_database)
    with exercise_database() as db:
        for index in range(15):
            db.add(
                Message(
                    session_id="mine",
                    role="user",
                    content="一条较晚的追问",
                    created_at=datetime(2026, 1, 2) + timedelta(seconds=index),
                )
            )
    service, model, _ = learning([decision()])
    # 此例只验证原文投影；语义摘要调用与失败协议由test_context_compaction独立验证。
    model.settings.context_compression_enabled = False
    asyncio.run(
        service.turn(
            "mine",
            MessageRequest(wait_id=current_wait(), content="第二问为什么不能重复？", request_id="followup-001"),
            "turn-1",
            emit,
        )
    )
    payload = json.loads(model.messages[0][1]["content"])
    active = payload["exercise_context"]["active_exercise"]
    assert (
        active["question"]["stem"] == contract["stem"] and len(contract["stem"]) > 1800
    )
    assert (
        active["exercise_id"] == "exercise-1" and active["status"] == "awaiting_answer"
    )
    # 最近四轮保留原文，早期完整消息进入历史区；不再在规划器提前截字。
    assert len(payload["recent_messages"]) == 8
    historical = [json.loads(message["content"])["closed_history_source"]
                  for message in model.messages[0]
                  if message["role"] == "user" and '"closed_history_source"' in message["content"]]
    assert historical
    with exercise_database() as db:
        assert all(item["data"]["content"] == db.get(Message, item["data"]["id"]).content for item in historical)
    assert all(
        "第二问禁止" not in message["content"] for message in payload["recent_messages"]
    )
    assert_public(payload["exercise_context"])


def test_teaching_agent_receives_the_same_public_question(exercise_database):
    contract = pose(exercise_database)
    service, model, _ = learning(
        [
            call(
                "call_knowledge_agent",
                task="解释这题的限制条件",
                concept_ids=["math:c1"],
            ),
            {
                "title": "计数条件",
                "content": "先识别题目中不能重复选择的条件，再计算每一步可用的物品数。第一次选走物品后，第二次需要排除已经选过的物品。",
                "follow_up": "第二步还有几种选择？",
                "next_action": "practice",
            },
            decision("teach", concept_ids=["math:c1"]),
        ]
    )
    asyncio.run(
        service.turn(
            "mine",
            MessageRequest(wait_id=current_wait(), content="解释这题的限制条件", request_id="followup-002"),
            "turn-2",
            emit,
        )
    )
    payload = json.loads(model.messages[1][1]["content"])
    assert (
        payload["exercise_context"]["active_exercise"]["question"]["stem"]
        == contract["stem"]
    )
    assert_public(payload["exercise_context"])
    returned = [
        json.loads(m["content"]) for m in model.messages[-1] if m["role"] == "tool"
    ][-1]
    assert (
        returned["agent"] == "knowledge" and returned["lesson"]["title"] == "计数条件"
    )
    with exercise_database() as db:
        assert db.get(LearningSession, "mine").pending_context["assisted"] is True


def test_history_tools_and_explanation_target_the_previous_exercise(exercise_database):
    previous_id = "00000000-0000-0000-0000-000000000001"
    original = pose(exercise_database, eid=previous_id)
    with exercise_database() as db:
        db.add(
            Concept(id="math:c2", course_id="math", source_id="c2", name="另一个知识点")
        )
        db.get(Question, "math:q2").concept_ids = ["math:c2"]
        db.get(LearningSession, "mine").concept_ids = ["math:c2"]
    pose(exercise_database, "math:q2", eid="exercise-2", second=2)
    service, model, _ = learning(
        [
            call("list_session_exercises", limit=1, offset=1),
            call("get_session_exercise", exercise_id=previous_id),
            call(
                "call_exercise_agent",
                operation="explain",
                task="上一题第二问为什么少一种？",
                concept_ids=["math:c1"],
                exercise_id=previous_id,
            ),
            {
                "content": "上一题第二问禁止重复，所以第二步剩余的选择会减少。",
                "key_points": ["不能重复"],
            },
            decision("explain", exercise_id=previous_id),
        ]
    )
    # 本例验证完整长题读取、历史题定位和子代理接续；固定脚本不理解预算收束。
    # 仅给本用例明确窗口，不依赖全局工具schema大小；默认窗口和原题保持不变。
    # 预算耗尽与禁用工具后的收束由context_compaction/tool_budget专测独立覆盖。
    model.settings.context_window_tokens = 65536
    model.settings.context_request_tokens = 65536
    original_explain = service.exercises.explain
    observed_contexts = []

    async def explain(contract, context, *args, **kwargs):
        observed_contexts.append(deepcopy(context))
        return await original_explain(contract, context, *args, **kwargs)

    service.exercises.explain = explain
    asyncio.run(
        service.turn(
            "mine",
            MessageRequest(wait_id=current_wait(), 
                content="上一题第二问为什么少一种？", request_id="followup-003"
            ),
            "turn-3",
            emit,
        )
    )
    tool_payload = json.loads(model.messages[2][-1]["content"])
    assert tool_payload["question"]["stem"] == original["stem"]
    assert_public(tool_payload)
    explanation = json.loads(model.messages[3][1]["content"])
    assert explanation["question_contract"]["id"] == "math:q1"
    assert explanation["user_question"] == "上一题第二问为什么少一种？"
    assert observed_contexts[0].concept_ids == original["concept_ids"]
    assert "math:q1" in observed_contexts[0].excluded_question_ids
    with exercise_database() as db:
        row = db.get(LearningSession, "mine")
        assert (
            row.pending_question_id == "math:q2"
            and row.pending_context["assisted"] is False
        )
        context = SessionExerciseService(db, "mine", "math", "local").context()
        assert context["active_exercise"]["exercise_id"] == previous_id
        assert context["pending_exercise"]["question"]["id"] == "math:q2"
    returned = [
        json.loads(m["content"]) for m in model.messages[-1] if m["role"] == "tool"
    ][-1]
    assert (
        returned["operation"] == "explain"
        and "上一题第二问" in returned["explanation"]["content"]
    )


def test_history_outside_recent_summaries_can_be_paged_and_read(exercise_database):
    for index in range(1, 6):
        pose(exercise_database, eid=f"exercise-{index}", second=index)
    with exercise_database() as db:
        history = SessionExerciseService(db, "mine", "math", "local")
        recent = history.context()["recent_exercises"]
        assert (
            recent["total"] == 5
            and recent["returned_count"] == 3
            and recent["has_more"]
        )
        older = history.list(limit=2, offset=3)
        assert [item["exercise_id"] for item in older["items"]] == [
            "exercise-2",
            "exercise-1",
        ]
        assert len(history.get("exercise-1")["question"]["stem"]) > 1800
        assert_public(older)


def test_old_snapshot_remains_readable_but_cannot_use_changed_answer(exercise_database):
    original = pose(exercise_database, archive=False)
    pose(exercise_database, "math:q2", eid="exercise-2", second=2)
    with exercise_database() as db:
        question = db.get(Question, "math:q1")
        question.stem, question.version, question.answer = "更新后的另一道题", 2, ["B"]
    with exercise_database() as db:
        history = SessionExerciseService(db, "mine", "math", "local")
        assert history.get("exercise-1")["question"]["stem"] == original["stem"]
        with pytest.raises(ValueError, match="版本已变化"):
            history.internal_contract("exercise-1")


def test_archived_private_contract_survives_catalog_changes(exercise_database):
    original = pose(exercise_database)
    pose(exercise_database, "math:q2", eid="exercise-2", second=2)
    with exercise_database() as db:
        question = db.get(Question, "math:q1")
        question.stem, question.version, question.answer = "更新题干", 2, ["B"]
    with exercise_database() as db:
        frozen = SessionExerciseService(db, "mine", "math", "local").internal_contract(
            "exercise-1"
        )
        assert frozen == original
    # 恢复接口与消息事件共用序列化器，私有快照不能进入前端或主控历史。
    restored = SessionService().get("mine")
    assert "_exercise_contract" not in json.dumps(restored, ensure_ascii=False)
    assert_public(restored)


@pytest.mark.parametrize("entrypoint", ["direct", "planned"])
def test_new_question_archives_contract_without_exposing_it(
    exercise_database, entrypoint
):
    with exercise_database() as db:
        contract = question_contract(db.get(Question, "math:q1"))
    service, _, _ = learning(
        [
            call(
                "call_exercise_agent",
                operation="select",
                task="选择计数练习",
                concept_ids=["math:c1"],
            ),
            decision("practice", concept_ids=["math:c1"]),
        ]
    )

    async def select_question(*args, **kwargs):
        return contract, {"citations": [], "run_id": "stub-selection-run"}

    async def predict(*args):
        return {"predicted_correct": True, "rationale": "测试预测"}

    service.exercises.select = select_question
    service.assessment.predict = predict
    events = []

    async def capture(event, data):
        events.append((event, data))

    if entrypoint == "direct":
        asyncio.run(
            service.execute("mine", {
                "kind": "practice_existing", "question_id": "math:q1",
                "cognitive_type": "recall", "request_id": "existing-question-001",
            }, "turn-pose", capture)
        )
    else:
        asyncio.run(
            service.turn(
                "mine",
                MessageRequest(wait_id=current_wait(), content="给我一道题", request_id="question-001"),
                "turn-pose",
                capture,
            )
        )
    with exercise_database() as db:
        history = SessionExerciseService(db, "mine", "math", "local")
        active = history.get()
        message = db.get(Message, active["exercise_id"])
        assert message.metadata_json["_exercise_contract"] == contract
        assert_public(history.context())
    assert "_exercise_contract" not in json.dumps(events, ensure_ascii=False)
    assert_public(SessionService().get("mine"))


def test_pending_question_uses_frozen_version_after_catalog_update(exercise_database):
    original = pose(exercise_database)
    with exercise_database() as db:
        question = db.get(Question, "math:q1")
        question.stem, question.version = "后来更新的题干", 2
    restored = SessionService().get("mine")
    assert restored["pending_question"]["stem"] == original["stem"]
    with exercise_database() as db:
        history = SessionExerciseService(db, "mine", "math", "local")
        assert history.internal_contract()["stem"] == original["stem"]
        assert_public(history.context())


def test_selection_excludes_seen_questions_and_preserves_unknown_catalog_label(exercise_database):
    service, model, _ = learning([{"question_id": "math:q2", "reason": "选择未练习候选。"}])
    with exercise_database() as db:
        db.add(Attempt(
            id="seen-attempt", session_id="mine", question_id="math:q1", answer_text="A",
            result="correct", score=1, assisted=False, assessment={}, state_updates=[],
            idempotency_key="seen-key",
        ))
    retrieved = []

    async def retrieve(query, **kwargs):
        retrieved.append(kwargs)
        return {"items": [{"question": service.catalog.question("math:q2", "math")}]}

    service.runner.tools.retrieval = SimpleNamespace(retrieve=retrieve)
    contract, _ = asyncio.run(service.exercises.select(
        ToolContext(course_id="math", session_id="mine", concept_ids=["math:c1"], request_id="select-seen"),
        "select-turn", "application", emit,
    ))
    assert retrieved[0]["excluded_question_ids"] == ["math:q1"]
    assert retrieved[0]["mode"] == "keyword"
    assert contract["id"] == "math:q2" and contract["cognitive_type"] == "unknown"


@pytest.mark.parametrize(
    "sid,course,student",
    [
        ("other", "math", "local"),
        ("mine", "mechanics", "local"),
        ("mine", "math", "someone"),
    ],
)
def test_history_rejects_another_student_or_course(
    exercise_database, sid, course, student
):
    with exercise_database() as db:
        with pytest.raises(ValueError, match="当前范围"):
            SessionExerciseService(db, sid, course, student)


def test_tools_cannot_read_another_session_or_override_identity(exercise_database):
    pose(exercise_database, sid="other", eid="other-exercise")
    _, _, registry = learning([])
    context = ToolContext(session_id="mine", course_id="math", student_id="local")
    runtime = ToolRuntime(
        state={}, context=RuntimeContext(scope=context, role="supervisor", run_id="test", root_run_id="test"),
        config={}, stream_writer=lambda value: None, tool_call_id="test", store=None,
    )
    tools = {item.name: item for item in registry.for_role("supervisor")}
    with pytest.raises(ValueError, match="不属于当前会话"):
        asyncio.run(tools["get_session_exercise"].coroutine(exercise_id="other-exercise", runtime=runtime))
    with pytest.raises(TypeError):
        asyncio.run(tools["list_session_exercises"].coroutine(session_id="other", runtime=runtime))
    assert "get_session_exercise" not in {item.name for item in registry.for_role("exercise")}


def test_repeated_question_does_not_attach_the_other_attempt(exercise_database):
    pose(exercise_database, eid="exercise-1", second=1)
    with exercise_database() as db:
        db.add(
            Attempt(
                id="old-attempt",
                session_id="mine",
                question_id="math:q1",
                answer_text="第一次的回答",
                result="incorrect",
                score=0,
                assisted=False,
                assessment={"confidence": 0.95},
                state_updates=[],
                idempotency_key="old-key",
                created_at=datetime(2026, 1, 1, 0, 0, 1, 500000),
            )
        )
    pose(exercise_database, eid="exercise-2", second=2)
    with exercise_database() as db:
        history = SessionExerciseService(db, "mine", "math", "local")
        assert history.get("exercise-1")["student_answer"] == "第一次的回答"
        assert history.get("exercise-2")["student_answer"] is None
        assert history.get("exercise-2")["status"] == "awaiting_answer"


def test_post_assessment_supervisor_receives_answered_question(exercise_database):
    original = pose(exercise_database)
    service, model, _ = learning([decision()])

    async def assess(session_id, answer, key, *args):
        diagnosis = {
            "result": "correct",
            "score": 1,
            "confidence": 0.95,
            "feedback": "正确",
            "reflection": "依据原始作答",
            "answer_key": ["PRIVATE_KEY"],
            "level": "beginner",
        }
        with exercise_database() as db:
            row = db.get(LearningSession, session_id)
            row.pending_question_id = None
            row.pending_context = {"last_contract": row.pending_context["contract"]}
            db.add(
                Attempt(
                    id="submitted-attempt",
                    session_id=session_id,
                    question_id="math:q1",
                    answer_text=answer,
                    result="correct",
                    score=1,
                    assisted=False,
                    assessment=diagnosis,
                    state_updates=[],
                    idempotency_key=key,
                )
            )
        return {"attempt_id":"submitted-attempt","question_id":"math:q1","result": "correct", "diagnosis": diagnosis, "state_updates": []}

    async def prepare(*args,**kwargs):
        return {}

    async def commit(session_id,answer,key,turn_id,emit,prepared,**kwargs):
        return await assess(session_id,answer,key,turn_id,emit)

    service.assessment.prepare = prepare
    service.assessment.commit = commit
    asyncio.run(service.answer(
        "mine", AnswerRequest(answer="我的原始回答", request_id="answer-key-1",
                              question_id="math:q1", wait_id=current_wait()),
        "turn-4", emit,
    ))
    payload = json.loads(model.messages[0][1]["content"])
    active = payload["exercise_context"]["active_exercise"]
    assert active["question"]["stem"] == original["stem"]
    assert active["student_answer"] == "我的原始回答" and active["status"] == "answered"
    assert "PRIVATE_KEY" not in json.dumps(payload)
    assert_public(payload["exercise_context"])
