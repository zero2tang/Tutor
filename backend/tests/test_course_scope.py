"""在独立内存数据库验证课程隔离，不调用外部模型或现有数据库。"""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.infrastructure.persistence.database import Base
from app.domains.courses.models import Concept, Course, Question
from app.domains.sessions.models import LearningSession
from app.domains.students.models import MemoryItem, StudentState
from app.domains.assessment.service import AssessmentService
from app.domains.courses.catalog import CatalogService, public_question, question_contract
from app.domains.assessment.grading import deterministic_grade
from app.application.learning.service import LearningService
from app.domains.sessions.service import SessionService
from app.domains.students.service import StudentService
from app.agent.tools.contracts import ToolContext
from app.application.agent.tools import TeachingTools
from native_tools import invoke_tool
from langgraph.checkpoint.memory import InMemorySaver


@pytest.fixture
def scoped_database(monkeypatch, tmp_path):
    # 文件数据库允许并发工具各自在线程内建立独立连接。
    engine = create_engine("sqlite:///" + (tmp_path / "scoped.db").as_posix())
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
        "app.application.learning.entry",
        "app.application.learning.actions",
        "app.application.learning.publication",
        "app.application.agent.tools",
        "app.domains.practice.service",
        "app.domains.assessment.service",
        "app.domains.students.service",
    ):
        monkeypatch.setattr(module + ".db_session", isolated_session)
    with isolated_session() as db:
        for course_id in ("mechanics", "math"):
            db.add(
                Course(
                    id=course_id,
                    title=course_id,
                    description="测试课程",
                    source_ref="fixture",
                )
            )
            db.add(
                Concept(
                    id=course_id + ":c1",
                    course_id=course_id,
                    source_id="c1",
                    name="同名知识点",
                )
            )
            db.add(
                Question(
                    id=course_id + ":q1",
                    course_id=course_id,
                    source_id="q1",
                    stem="选择正确答案",
                    options={"A": "甲", "B": "乙"},
                    answer=["A"],
                    question_type="choice",
                    cognitive_type="unknown",
                    concept_ids=[course_id + ":c1"],
                    explanation="服务端解析",
                    source_ref="fixture",
                    validation={},
                )
            )
    yield isolated_session
    engine.dispose()


def test_catalog_rejects_unknown_course_and_cross_course_objects(scoped_database):
    catalog = CatalogService()
    assert catalog.course("math")["id"] == "math"
    with pytest.raises(ValueError, match="课程不存在"):
        catalog.concepts("math_unimported")
    with pytest.raises(ValueError, match="不属于当前课程"):
        catalog.question("mechanics:q1", "math")
    with pytest.raises(ValueError, match="其他课程知识点"):
        catalog.resolve_concepts("", "math", ["mechanics:c1"])


def test_math_session_and_history_keep_course_scope(scoped_database):
    sessions = SessionService()
    mechanical = sessions.create("力学记录", ["mechanics:c1"])
    mathematical = sessions.create("数学记录", ["math:c1"], "math")
    assert mathematical["course_id"] == "math"
    assert [item["id"] for item in sessions.list("math")] == [mathematical["id"]]
    assert {item["id"] for item in sessions.list()} == {
        mechanical["id"],
        mathematical["id"],
    }
    with pytest.raises(ValueError):
        sessions.create("混入其他课", ["mechanics:c1"], "math")


def test_math_public_images_keep_markdown_and_hide_private_validation():
    question = Question(
        id="math:q1",
        course_id="math",
        source_id="q1",
        stem="如图 ![图](/api/assets/math/question_1-image_0.png)",
        options={},
        answer=["24"],
        explanation="私有解析",
        question_type="fill",
        cognitive_type="unknown",
        concept_ids=["math:c1"],
        source_ref="fixture",
        generated=False,
        version=1,
        validation={
            "images": [
                "question_1-image_0.png",
                "../secret.png",
                "https://example.com/leak.png",
            ],
            "figure_available": True,
            "stem_html": "<script>secret</script>",
            "analysis_images": ["analysis_1-image_0.png"],
        },
    )
    public = public_question(question)
    assert public["availability"] == "ready"
    assert public["images"] == [
        {"url": "/api/assets/math/question_1-image_0.png", "alt": "题目配图"}
    ]
    assert public["stem"] == question.stem
    assert not (
        {"answer", "explanation", "validation", "stem_html", "analysis_images"}
        & public.keys()
    )
    question.validation = {
        **question.validation,
        "availability": "ambiguous_options",
        "availability_note": "原始图片选项重复",
    }
    assert public_question(question)["availability"] == "ambiguous_options"
    question.validation["availability"] = "invalid_answer"
    assert public_question(question)["availability"] == "invalid_answer"


@pytest.mark.parametrize(
    "answer,correct", [("60；20", True), ("20；60", False), ("60", False)]
)
def test_ordered_math_blanks_cannot_be_swapped_or_omitted(answer, correct):
    result = deterministic_grade(
        {
            "answer": {"1": ["$$60$$"], "2": ["$$20$$"]},
            "question_type": "fill",
            "options": {},
        },
        answer,
    )
    assert bool(result and result["result"] == "correct") is correct


def test_student_profile_never_mixes_other_course_evidence(scoped_database):
    with scoped_database() as db:
        for course_id in ("mechanics", "math"):
            db.add(
                StudentState(
                    id="local:" + course_id + ":c1",
                    student_id="local",
                    concept_id=course_id + ":c1",
                    mastery={"beginner": 0.5, "advanced": None, "proficient": None},
                    evidence_ids=[course_id + ":attempt"],
                    evidence_questions=[course_id + ":q1"],
                )
            )
            db.add(
                MemoryItem(
                    course_id=course_id,
                    concept_id=course_id + ":c1",
                    kind="misconception",
                    text=course_id + "错误证据",
                    evidence_ids=[course_id + ":attempt"],
                )
            )
    profile = StudentService().profile("math", ["math:c1"])
    assert profile["course_id"] == "math"
    assert {state["concept_id"] for state in profile["states"]} == {"math:c1"}
    assert {memory["text"] for memory in profile["memories"]} == {"math错误证据"}
    with pytest.raises(ValueError):
        StudentService().profile("math", ["mechanics:c1"])


def test_tool_uses_session_course_and_rejects_foreign_targets(scoped_database):
    observed = []

    async def retrieve(query, **kwargs):
        observed.append(kwargs)
        return {"items": [], "channels_used": []}

    registry = TeachingTools(SimpleNamespace(retrieve=retrieve))
    context = ToolContext(
        course_id="math", session_id="math-session", concept_ids=["math:c1"]
    )
    asyncio.run(
        invoke_tool(registry,"knowledge", "rag_retrieve", {"query": "知识点"}, context)
    )
    assert observed[0]["course_id"] == "math"
    with pytest.raises(ValueError):
        asyncio.run(
            invoke_tool(registry,
                "knowledge",
                "rag_retrieve",
                {"query": "知识点", "concept_ids": ["mechanics:c1"]},
                context,
            )
        )
    assert len(observed) == 1


def test_explicit_practice_takes_course_from_persisted_session(scoped_database):
    session = SessionService().create("数学", ["math:c1"], "math")
    runner = SimpleNamespace()
    learning = LearningService(runner, checkpointer=InMemorySaver())
    selected_contexts = []

    async def select_question(context, *args, **kwargs):
        selected_contexts.append(context)
        return (
            CatalogService().question(
                kwargs["explicit_question_id"], context.course_id, internal=True
            ),
            {},
        )

    async def emit(*args):
        pass

    learning.exercises.select = select_question
    result = asyncio.run(
        learning.execute(session["id"], {"kind": "practice_existing", "question_id": "math:q1",
            "cognitive_type": "recall", "request_id": "practice-math"}, "turn-math", emit)
    )
    assert result["question"]["course_id"] == "math"
    assert selected_contexts[0].course_id == "math"
    with pytest.raises(ValueError):
        asyncio.run(
            learning.execute(session["id"], {"kind": "practice_existing", "question_id": "mechanics:q1",
                "cognitive_type": "recall", "request_id": "practice-foreign"}, "turn-foreign", emit)
        )
    assert len(selected_contexts) == 1


def test_assessment_writes_math_evidence_and_memory_only(scoped_database):
    session = SessionService().create("数学", ["math:c1"], "math")
    with scoped_database() as db:
        question = db.get(Question, "math:q1")
        row = db.get(LearningSession, session["id"])
        row.active_turn_id = "turn-1"
        row.pending_question_id = question.id
        row.pending_context = {
            "contract": question_contract(question),
            "target_concept_ids": ["math:c1"],
            "cognitive_type": "recall",
            "assisted": False,
        }
    contexts = []

    async def run(*args, **kwargs):
        contexts.append(args[4])
        return {
            "output": {
                "result": "incorrect",
                "score": 0,
                "confidence": 0.95,
                "level": "beginner",
                "suggested_delta": -0.1,
                "feedback": "需要再练",
                "reflection": "从错误答案发现混淆",
                "misconception": "数学测试误解",
                "next_action": "teach",
            },
            "run_id": "fake-run",
        }

    async def emit(*args):
        pass

    assessment = AssessmentService(SimpleNamespace(run=run))
    result = asyncio.run(
        assessment.assess(session["id"], "B", "submission-1", "turn-1", emit)
    )
    assert contexts[0].course_id == "math"
    assert result["state_updates"][0]["concept_id"] == "math:c1"
    with scoped_database() as db:
        assert db.get(StudentState, "local:mechanics:c1") is None
        assert db.scalar(select(MemoryItem)).course_id == "math"
