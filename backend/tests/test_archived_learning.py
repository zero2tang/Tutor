"""固定历史档案与课程初始化；旧证据不得重算为当前掌握度。"""

from datetime import datetime
import json
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select

from app.infrastructure.persistence import database
from app.infrastructure.persistence.database import Base, db_session
from app.domains.courses.models import Concept, Course, Question
from app.domains.courses.bootstrap import COURSE_ID, seed_course
from app.domains.students import archive
from scripts.maintenance.simplify_p0 import historical_summary


@pytest.fixture
def archived_db(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "archive.db"))
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", lambda: engine)
    yield
    engine.dispose()


def test_seed_is_repeatable_and_does_not_generate_or_rewrite_questions(archived_db):
    seed_course()
    seed_course()
    with db_session() as db:
        assert db.scalar(select(func.count()).select_from(Course)) == 1
        assert db.scalar(select(func.count()).select_from(Concept)) == 4
        assert db.scalar(select(func.count()).select_from(Question)) == 0
        db.get(Course, COURSE_ID).description = "用户已有说明"
        db.add(Question(id="old-question", course_id=COURSE_ID, source_id="old", stem="已有题目",
            answer=["private"], question_type="fill", options={}, concept_ids=[],
            explanation="历史解析", source_ref="fixture://old"))
    seed_course()
    with db_session() as db:
        assert db.get(Course, COURSE_ID).description == "用户已有说明"
        assert db.get(Question, "old-question").answer == ["private"]


def evidence(question, kind="completion", independent=False):
    return {"id": str(uuid4()), "question_id": question, "kind": kind,
        "created_at": datetime.now(), "independent": independent, "simulated": True,
        "details": {"stage": "transfer", "assisted": not independent,
            "verdict": {"completion": {"evidence_scope": "equation_steps"}}}}


def test_archive_preserves_simulation_scope_and_disputes_without_mastery():
    result = historical_summary([evidence("q1", independent=True), evidence("q1", independent=True),
        evidence("q2"), evidence("q3", independent=True), evidence("q3", kind="dispute")])
    assert result["independent_transfer_count"] == 1
    assert result["assisted_completion_count"] == 1
    assert result["pending_review_count"] == 1 and result["observed_steps"] == 4
    assert result["mastery_probability"] is None
    assert all(r["simulated"] and r["scope"] == "equation_steps" for r in result["evidence"])
    assert all(r["question_id"] != "q3" for r in result["evidence"])


def test_online_archive_is_frozen_and_does_not_read_legacy_sql(tmp_path, monkeypatch):
    path = tmp_path / "summaries.json"
    expected = {**historical_summary([evidence("q1")]), "archive_version": 1}
    path.write_text(json.dumps({"students": {"student": expected}}), encoding="utf-8")
    monkeypatch.setattr(archive, "ARCHIVE", path)
    archive._summaries.cache_clear()
    try:
        assert archive.archived_summary("student") == expected
        assert archive.archived_summary("unknown") is None
        assert not {"tutoring_actions", "tutoring_evidence"}.intersection(Base.metadata.tables)
    finally:
        archive._summaries.cache_clear()


def test_new_profile_does_not_restore_archived_student_history(archived_db, monkeypatch):
    """资源迁移后，原本机身份相同也不能回填旧学习档案。"""
    from app.domains.students.service import StudentService

    seed_course()

    def forbidden_archive(student_id):
        raise AssertionError("新学习状态不得读取旧学生归档")

    monkeypatch.setattr(archive, "archived_summary", forbidden_archive)
    result = StudentService().profile(course_id=COURSE_ID, student_id="local")
    assert "step_learning" not in result
    assert result["attempt_count"] == 0
    assert result["assessed_concepts"] == 0
    assert result["memories"] == []
    assert all(row["status"] == "unassessed" for row in result["states"])
    assert all(value is None for row in result["states"] for value in row["mastery"].values())
