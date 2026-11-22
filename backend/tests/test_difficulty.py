"""唯一题目难度来源、版本失效、未知与公开字段边界。"""

from contextlib import contextmanager
from copy import deepcopy
import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.infrastructure.persistence.database import Base
from app.domains.courses.models import Course, Concept, Question
from app.domains.courses.difficulty.metadata import fingerprint, estimate
from app.domains.courses.difficulty.service import DifficultyQueryService


@pytest.fixture
def database(tmp_path):
    engine = create_engine("sqlite:///"+(tmp_path/"difficulty.db").as_posix())
    Base.metadata.create_all(engine)
    @contextmanager
    def db_session():
        with Session(engine,expire_on_commit=False) as db:
            yield db
            db.commit()
    with db_session() as db:
        for course in ("math","other"):
            db.add(Course(id=course,title=course,description="自编测试",source_ref="fixture"))
            db.add(Concept(id=course+":c1",course_id=course,source_id="c1",name="计数"))
        for index,value in enumerate((20,80,None)):
            row = Question(id="q"+str(index),course_id="math",source_id=str(index),stem="自编计数问题",options={},
                answer=["PRIVATE_ANSWER"],explanation="PRIVATE_EXPLANATION",question_type="fill",concept_ids=["math:c1"],source_ref="fixture")
            db.add(row)
            db.flush()
            row.difficulty_estimate = {"value":value,"status":"estimated" if value is not None else "unknown",
                "method":"content_rubric","version":"v1","reference_group":"自编学习者", "model":"test", "confidence":"medium",
                "scale_max":100,"higher_is_harder":True,"source_hash":"original-hash","question_fingerprint":fingerprint(row)}
    yield db_session
    engine.dispose()


def test_single_source_orders_questions_without_answers(database):
    result = DifficultyQueryService(database).query("math")
    assert [r["id"] for r in result["items"]] == ["q1","q0"]
    assert result["scored_count"]==2 and result["matched_count"]==2
    assert "PRIVATE_ANSWER" not in json.dumps(result) and "PRIVATE_EXPLANATION" not in json.dumps(result)


def test_unknown_is_not_zero(database):
    result = DifficultyQueryService(database).detail("math",entity_id="q2")
    assert result["value"] is None and result["status"]=="unknown"


@pytest.mark.parametrize("field",["stem","answer","version"])
def test_changed_question_invalidates_old_estimate(database,field):
    with database() as db:
        row=db.get(Question,"q1")
        setattr(row,field,row.version+1 if field=="version" else ["changed"] if field=="answer" else "新题干")
    result = DifficultyQueryService(database).detail("math",entity_id="q1")
    assert result["value"] is None and result["status"]=="stale"
    assert [q["id"] for q in DifficultyQueryService(database).query("math")["items"]]==["q0"]


def test_concept_filter_and_paging_remain_supported(database):
    result = DifficultyQueryService(database).query("math",concept_ids=["math:c1"],limit=1,order="asc")
    assert result["items"][0]["id"]=="q0" and result["has_more"]
    assert not DifficultyQueryService(database).query("math",excluded_question_ids=["q1","q0"])["items"]


def test_foreign_course_and_concept_rejected(database):
    with pytest.raises(ValueError,match="课程"):
        DifficultyQueryService(database).detail("other",entity_id="q0")
    with pytest.raises(ValueError,match="课程"):
        DifficultyQueryService(database).query("math",concept_ids=["other:c1"])


@pytest.mark.parametrize("value",[-1,101,float("nan"),True])
def test_invalid_numeric_ranges_rejected(database,value):
    with pytest.raises(ValueError):
        DifficultyQueryService(database).query("math",min_difficulty=value)


def test_concept_difficulty_and_profile_api_retired(database):
    with pytest.raises(ValueError,match="知识点"):
        DifficultyQueryService(database).detail("math",entity_type="concept",entity_id="math:c1")
    from app.api.routes.difficulty import router
    app=FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/api/difficulty/profiles").status_code==410
