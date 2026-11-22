"""独立验证评估边界，避免用接口成功代替状态正确。"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.infrastructure.persistence.database import Base
from app.domains.courses.models import Question
from app.domains.students.models import StudentState
from app.domains.courses.catalog import public_question
from app.domains.assessment.grading import deterministic_grade
from app.domains.students.service import StudentService
from app.agent.tools.contracts import ToolContext
from app.application.agent.tools import TeachingTools
from native_tools import invoke_tool


def test_choice_and_partial_credit_are_deterministic():
    question = {
        "question_type": "multi_choice",
        "options": {"A": "甲", "B": "乙", "C": "丙"},
        "answer": ["A", "C"],
    }
    assert deterministic_grade(question, "C,A")["result"] == "correct"
    assert deterministic_grade(question, "A")["result"] == "partial"
    assert deterministic_grade(question, "A,B")["result"] == "incorrect"


def test_ambiguous_single_choice_does_not_get_an_arbitrary_grade():
    question = {
        "question_type": "choice",
        "options": {"A": "甲", "B": "乙"},
        "answer": ["A"],
    }
    assert deterministic_grade(question, "A,B")["result"] == "ungraded"
    assert deterministic_grade(question, "尚不确定，应如何理解") is None


def test_missing_figure_is_visible_but_no_answer_is_exposed():
    question = Question(
        id="q",
        source_id="q",
        course_id="mechanics",
        stem="如图所示的弹簧系统",
        options={"A": "a"},
        answer=["A"],
        concept_ids=["mechanics:c197"],
        source_ref="dataset",
        generated=False,
        question_type="choice",
        cognitive_type="unknown",
        version=1,
        validation={},
    )
    public = public_question(question)
    assert public["availability"] == "missing_figure"
    assert "answer" not in public and "explanation" not in public


@pytest.mark.parametrize(
    "assisted,result,confidence",
    [(True, "correct", 1.0), (False, "ungraded", 1.0), (False, "correct", 0.6)],
)
def test_insufficient_evidence_does_not_touch_database(assisted, result, confidence):
    assert (
        StudentService.apply_evidence(
            object(),
            concept_ids=["c"],
            attempt_id="a",
            question_id="q",
            diagnosis={"result": result, "confidence": confidence},
            assisted=assisted,
        )
        == []
    )


def test_same_question_cannot_accumulate_independent_evidence():
    engine = create_engine("sqlite://")
    StudentState.__table__.create(engine)
    diagnosis = {
        "result": "correct",
        "confidence": 0.95,
        "level": "beginner",
        "suggested_delta": 0.25,
        "misconception": "",
    }
    with Session(engine) as db:
        first = StudentService.apply_evidence(
            db,
            concept_ids=["c"],
            attempt_id="a1",
            question_id="q1",
            diagnosis=diagnosis,
            assisted=False,
        )
        db.flush()
        repeated = StudentService.apply_evidence(
            db,
            concept_ids=["c"],
            attempt_id="a2",
            question_id="q1",
            diagnosis=diagnosis,
            assisted=False,
        )
        assert first and repeated == []
        state = db.scalar(select(StudentState))
        assert state.evidence_ids == ["a1"] and state.evidence_questions == ["q1"]
        assert state.mastery["advanced"] is None and state.mastery["proficient"] is None


def test_tool_role_cannot_request_arbitrary_write():
    registry = TeachingTools(None)
    import asyncio

    with pytest.raises(PermissionError):
        asyncio.run(
            invoke_tool(registry,"knowledge", "commit_student_state", {}, ToolContext())
        )
