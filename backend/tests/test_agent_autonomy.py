"""在独立数据库验证自主工具循环、澄清延续和目标切换，不调用远程模型。"""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.infrastructure.persistence.database import Base
from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.assessment.models import Attempt
from app.domains.courses.models import Concept, ConceptRelation, Course, Question
from app.domains.sessions.models import LearningSession
from app.domains.students.models import StudentState
from app.domains.students.service import StudentService
from app.application.agent.tools import compact_tool_result
from native_runtime_helpers import LoopState, run_agent_loop
from app.application.agent.prompts import build_instruction
from app.application.agent.runner import AgentRunner
from app.agent.runtime.skills import SkillLibrary
from app.domains.learning.schemas import MessageRequest, Plan
from app.domains.courses.queries import CourseQueryService, graph_levels
from app.application.learning.service import LearningService
from app.domains.sessions.service import SessionService
from app.agent.tools.contracts import ToolContext
from app.application.agent.tools import TeachingTools
from native_tools import invoke_tool


@pytest.fixture
def autonomous_database(monkeypatch, tmp_path):
    # 文件数据库允许并发工具各自在线程内建立独立连接。
    engine = create_engine("sqlite:///" + (tmp_path / "autonomy.db").as_posix())
    Base.metadata.create_all(engine)

    @contextmanager
    def isolated_session():
        with Session(engine, expire_on_commit=False) as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    for module in (
        "app.domains.courses.catalog",
        "app.domains.sessions.service",
        "app.application.learning.service",
        "app.application.learning.planning",
        "app.application.learning.entry",
        "app.application.learning.receipts",
        "app.application.learning.actions",
        "app.application.learning.publication",
        "app.domains.courses.queries",
        "app.domains.courses.difficulty.service",
        "app.domains.practice.service",
        "app.domains.assessment.service",
        "app.domains.students.service",
    ):
        monkeypatch.setattr(module + ".db_session", isolated_session, raising=False)
    monkeypatch.setattr("app.application.agent.runner.db_session", isolated_session)
    monkeypatch.setattr("app.application.agent.tools.db_session", isolated_session)
    with isolated_session() as db:
        for course_id in ("math", "mechanics"):
            db.add(
                Course(
                    id=course_id,
                    title=course_id,
                    description="独立测试课程",
                    source_ref="fixture",
                )
            )
            for identifier, name, category in (
                ("c875", "知识点", True),
                ("c1", "加乘原理", False),
                ("c2", "容斥原理", False),
                ("c3", "综合计数", False),
            ):
                db.add(
                    Concept(
                        id=f"{course_id}:{identifier}",
                        source_id=identifier,
                        course_id=course_id,
                        name=name,
                        topic_type="category" if category else "concept",
                        summary="教学测试内容",
                    )
                )
            for index in range(1, 4):
                db.add(
                    Question(
                        id=f"{course_id}:q{index}",
                        source_id=f"q{index}",
                        course_id=course_id,
                        stem="从三个不同物品中选一个有多少种方法？",
                        options={},
                        answer=["3"],
                        explanation="每种选择相互排斥，共三种。",
                        question_type="fill",
                        cognitive_type="unknown",
                        concept_ids=[f"{course_id}:c{index}"],
                        source_ref="fixture",
                        validation={},
                    )
                )
        db.add(
            ConceptRelation(
                id="taxonomy-1",
                course_id="math",
                source_id="math:c875",
                target_id="math:c1",
                relation_type="HAS_CHILD",
                origin="fixture",
                verified=True,
            )
        )
        db.add(
            ConceptRelation(
                id="taxonomy-2",
                course_id="math",
                source_id="math:c1",
                target_id="math:c2",
                relation_type="HAS_CHILD",
                origin="fixture",
                verified=True,
            )
        )
    yield isolated_session
    engine.dispose()


def final(action="respond", ids=None, **kwargs):
    return {
        "action": action,
        "concept_ids": ids or [],
        "message": "请确认选择标准。",
        "steps": [],
        "rationale": "根据实际请求处理",
        "continuation": {
            "status": (
                "awaiting_clarification" if action == "clarify" else "awaiting_student"
            ),
            "current_step": "完成本轮指定步骤",
            "next_step": "等待学生反馈后继续当前学习目标。",
            "completed_steps": [],
        },
        **kwargs,
    }


def tool(tool_name, **arguments):
    return {
        "role": "assistant",
        "content": "",
        "reasoning_content": "保留的服务商推理字段",
        "tool_calls": [
            {
                "id": "call-" + tool_name,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
    }


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.settings = SimpleNamespace(llm_model="scripted-test")

    async def chat(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), kwargs))
        response = next(self.responses)
        message = (
            response
            if "tool_calls" in response
            else {
                "role": "assistant",
                "content": json.dumps(response, ensure_ascii=False),
            }
        )
        return {
            "choices": [{"message": message}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


def learning_with(responses):
    async def unexpected_retrieval(*args, **kwargs):
        raise AssertionError("这些任务不应自动把用户原句送去 RAG")

    model = ScriptedModel(responses)
    registry = TeachingTools(SimpleNamespace(retrieve=unexpected_retrieval))
    return LearningService(AgentRunner(model, registry)), model


async def emit(*args):
    pass


def request(text, key="request-0001", **kwargs):
    return MessageRequest(content=text, request_id=key, **kwargs)


def test_generic_goal_does_not_bind_the_taxonomy_root(autonomous_database):
    session = SessionService().create("我想学当前课程最难的知识点", [], "math")
    assert session["concept_ids"] == []


def test_clarification_needs_no_tool_or_teaching_and_is_persisted(autonomous_database):
    session = SessionService().create("我想学最难的几个知识点", [], "math")
    learning, model = learning_with(
        [
            final(
                "clarify",
                clarification={
                    "objective": "学习最难的三个知识点",
                    "missing_information": "难度的含义",
                    "options": ["数值难度", "先修链靠后"],
                },
            )
        ]
    )
    result = asyncio.run(
        learning.turn(
            session["id"], request(session["goal"], intent="teach"), "turn-1", emit
        )
    )
    restored = SessionService().get(session["id"])
    assert result["message"]["agent"] == "supervisor"
    assert restored["phase"] == "awaiting_clarification"
    assert restored["pending_decision"]["objective"] == "学习最难的三个知识点"
    assert result["message"]["metadata"]["clarification"]["options"] == [
        "数值难度",
        "先修链靠后",
    ]
    assert restored["concept_ids"] == [] and len(model.calls) == 1
    with autonomous_database() as db:
        assert (
            db.scalar(select(Attempt)) is None
            and db.scalar(select(StudentState)) is None
        )
        assert db.scalar(select(AgentRun)).tool_calls == []


def test_followup_uses_pending_decision_and_more_than_two_tool_rounds(
    autonomous_database,
):
    session = SessionService().create("最难的三个知识点", [], "math")
    learning, model = learning_with(
        [
            final(
                "clarify",
                clarification={
                    "objective": "最难的三个知识点",
                    "missing_information": "选择标准",
                    "options": ["难度数值", "先修层级"],
                },
            ),
            tool("read_skill", name="difficulty_and_graph"),
            tool("get_course_overview"),
            tool("rank_learning_targets", criterion="difficulty", top_k=3),
            final(
                message="课程尚无数值难度估计，不能伪造最高排名；可以先进行内容难度估计。"
            ),
        ]
    )
    asyncio.run(
        learning.turn(session["id"], request("最难的三个知识点"), "turn-1", emit)
    )
    result = asyncio.run(
        learning.turn(
            session["id"], request("按数值最高的来", "request-0002",
                wait_id=learning.sessions.get(session["id"])["pending_interaction"]["wait_id"]), "turn-2", emit
        )
    )
    payload = json.loads(model.calls[1][0][1]["content"])
    assert payload["pending_decision"]["objective"] == "最难的三个知识点"
    assert result["plan"]["action"] == "respond"
    assert SessionService().get(session["id"])["pending_decision"] is None
    with autonomous_database() as db:
        run = db.scalar(select(AgentRun).where(AgentRun.turn_id == "turn-2"))
        assert [c["name"] for c in run.tool_calls] == [
            "read_skill",
            "get_course_overview",
            "rank_learning_targets",
        ]
    last_tools = [
        json.loads(m["content"]) for m in model.calls[-1][0] if m["role"] == "tool" and m["content"].startswith("{")
    ]
    assert last_tools[-1]["error_kind"] == "request_rejected"


def test_invalid_root_target_can_be_repaired_by_the_model(autonomous_database):
    session = SessionService().create("讲解加乘原理", [], "math")
    lesson = {
        "title": "加乘原理",
        "content": "分类计数时将互不重叠的情况相加，分步计数时将每一步的选择数相乘，需要注意各阶段条件。",
        "follow_up": "先分类还是分步骤？",
        "next_action": "practice",
    }
    learning, model = learning_with(
        [
            final("teach", ["math:c875"]),
            tool("search_concepts", query="加乘"),
            tool("call_knowledge_agent", task="讲解加乘原理", concept_ids=["math:c1"]),
            lesson,
            final("teach", ["math:c1"]),
        ]
    )
    result = asyncio.run(
        learning.turn(session["id"], request("讲解加乘原理"), "turn-1", emit)
    )
    assert result["plan"]["concept_ids"] == ["math:c1"]
    assert "分类目录" in model.calls[1][0][-1]["content"]
    assert result["message"]["agent"] == "supervisor"
    returned = [
        json.loads(m["content"]) for m in model.calls[-1][0] if m["role"] == "tool" and m["content"].startswith("{")
    ][-1]
    assert returned["agent"] == "knowledge" and returned["lesson"] == {
        **lesson,
        "exercise_batch": None,
    }
    assert (
        result["message"]["metadata"]["delegations"][0]["tool"]
        == "call_knowledge_agent"
    )


def test_topic_switch_is_not_locked_to_previous_focus(autonomous_database):
    session = SessionService().create("学习加乘原理", ["math:c1"], "math")
    learning, model = learning_with(
        [
            tool("search_concepts", query="容斥"),
            tool(
                "call_knowledge_agent",
                task="切换目标，讲解容斥原理",
                concept_ids=["math:c2"],
            ),
            {
                "title": "容斥原理",
                "content": "合并两个集合的数量时需要减去重复计算的交集部分，这样每个元素才恰好计算一次。",
                "follow_up": "为什么减去交集？",
                "next_action": "practice",
            },
            final(
                "teach",
                ["math:c2"],
                goal_update="学习容斥原理",
                goal_update_request="换个目标，我要学容斥原理",
            ),
        ]
    )
    asyncio.run(
        learning.turn(
            session["id"], request("换个目标，我要学容斥原理"), "turn-1", emit
        )
    )
    restored = SessionService().get(session["id"])
    assert restored["concept_ids"] == ["math:c2"] and restored["goal"] == "学习容斥原理"
    child = json.loads(model.calls[2][0][1]["content"])
    assert child["concepts"] == [{"id": "math:c2", "name": "容斥原理"}]
    assert "容斥原理" in child["teaching_request"]
    assert any(
        m["role"] == "tool" and json.loads(m["content"]).get("agent") == "knowledge"
        for m in model.calls[-1][0]
    )


def test_concept_explanation_cannot_be_routed_to_missing_exercise(autonomous_database):
    session = SessionService().create("解释容斥原理", [], "math")
    learning, model = learning_with(
        [
            final("explain", ["math:c2"]),
            tool(
                "call_knowledge_agent",
                task="解释容斥原理的概念，而不是题解",
                concept_ids=["math:c2"],
            ),
            {
                "title": "容斥原理",
                "content": "把两个集合的元素数相加时，交集中的每一个元素都计算了两次，所以需要减去一次交集的数量。",
                "follow_up": "交集计算了几次？",
                "next_action": "practice",
            },
            final("teach", ["math:c2"]),
        ]
    )
    result = asyncio.run(
        learning.turn(session["id"], request("解释容斥原理"), "turn-1", emit)
    )
    assert "lesson" in result
    assert "explain 专指习题题解" in model.calls[1][0][-1]["content"]


def test_skill_progressive_loading_and_role_scope():
    prompt = build_instruction("supervisor", Plan, "auto")
    assert "goal_selection" in prompt
    assert "用 clarification 保存原始目标" not in prompt
    library = SkillLibrary()
    assert (
        "用 clarification 保存原始目标"
        in library.read("supervisor", "goal_selection")["instructions"]
    )
    with pytest.raises(ValueError):
        library.read("supervisor", "../assessment/diagnose")


def test_course_tools_exclude_root_and_do_not_substitute_relation_types(
    autonomous_database,
):
    queries = CourseQueryService(student_profile=StudentService().profile)
    result = queries.search("math")
    assert result["total_concepts"] == 4 and result["matched_count"] == 3
    assert all(c["topic_type"] == "concept" for c in result["items"])
    assert not queries.rank("math", criterion="difficulty")["available"]
    assert not queries.rank("math", criterion="prerequisite_depth")["available"]
    assert (
        queries.rank("math", criterion="taxonomy_depth")["items"][0]["id"] == "math:c2"
    )
    assert not queries.rank("math", criterion="personal_gap")["available"]


def test_question_numeric_estimates_do_not_create_concept_difficulty(autonomous_database):
    result = CourseQueryService().rank("math", criterion="difficulty")
    assert result["available"] is False
    assert result["items"] == []


def test_longest_prerequisite_chain_and_cycle_detection():
    assert graph_levels([("a", "b"), ("a", "c"), ("b", "c")])["c"] == 2
    with pytest.raises(ValueError, match="环"):
        graph_levels([("a", "b"), ("b", "a")])


def test_tool_error_is_returned_to_model_for_recovery(autonomous_database):
    session = SessionService().create("浏览课程", [], "math")
    learning, model = learning_with(
        [
            tool("search_concepts", limit=1000),
            tool("search_concepts", limit=2),
            final(message="课程中可以选择加乘原理或容斥原理。"),
        ]
    )
    asyncio.run(learning.turn(session["id"], request("浏览课程"), "turn-1", emit))
    failed = json.loads(model.calls[1][0][-1]["content"])
    assert failed["error"] in {"ValueError", "ValidationError"}
    assert json.loads(model.calls[2][0][-1]["content"])["returned_count"] == 2


def test_compaction_preserves_json_and_scope_notice():
    original = {
        "nodes": [{"id": str(i), "text": "x" * 11000} for i in range(25)],
        "total_nodes": 100,
    }
    compact = json.loads(json.dumps(compact_tool_result(original, tool_name="rag_retrieve")))
    assert len(compact["nodes"]) == 20 and compact["total_nodes"] == 100
    assert compact["context_truncated"] is True


def test_course_tool_arguments_cannot_override_the_persisted_course(
    autonomous_database,
):
    registry = TeachingTools(SimpleNamespace())
    with pytest.raises(ValueError):
        asyncio.run(
            invoke_tool(registry,
                "supervisor",
                "get_course_overview",
                {"course_id": "mechanics"},
                ToolContext(course_id="math"),
            )
        )


def test_tool_budget_finishes_with_explicit_partial_result(autonomous_database):
    model = ScriptedModel(
        [
            tool("get_course_overview"),
            tool("search_concepts"),
            final(message="已经取得部分信息，后续可以继续浏览。"),
        ]
    )
    registry = TeachingTools(SimpleNamespace())
    state = LoopState()
    result = asyncio.run(
        run_agent_loop(
            model,
            registry,
            "supervisor",
            [],
            Plan,
            ToolContext(course_id="math"),
            state,
            max_rounds=2,
            max_calls=2,
        )
    )
    assert result["action"] == "respond" and len(state.trace) == 2
    assert all(item["function"]["name"][0].isupper() for item in model.calls[-1][1]["tools"])


def test_repeated_skill_read_keeps_context_small_and_checks_parameters(
    autonomous_database,
):
    model = ScriptedModel(
        [
            tool("read_skill", name="goal_selection"),
            tool("read_skill", name="goal_selection"),
            tool("read_skill", name="goal_selection", course_id="mechanics"),
            final(),
        ]
    )
    state = LoopState()
    asyncio.run(
        run_agent_loop(
            model,
            TeachingTools(SimpleNamespace()),
            "supervisor",
            [],
            Plan,
            ToolContext(course_id="math"),
            state,
        )
    )
    assert state.trace[1]["cached"] is True
    assert state.trace[2]["status"] == "failed"
