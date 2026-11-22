"""跨目录、索引、检索和练习验证同一题库策略；不重写各层的序列化契约。"""

from contextlib import contextmanager
from types import SimpleNamespace
import asyncio
import base64
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.infrastructure.persistence.database import Base
from app.domains.courses.models import Course, Question
from app.domains.courses.questions.policy import access, can_search, require_access, searchable_conditions
from app.domains.courses.catalog import CatalogService, public_question, question_contract
from app.domains.courses.retrieval import indexing
from app.domains.courses.retrieval.service import RetrievalService
from app.domains.courses.retrieval.channels import run_channels
from app.domains.courses.retrieval.fusion import fuse
from app.domains.courses.questions.search.store import QuestionSearchStore
from app.application.question_retrieval.service import QuestionRetrievalService
from app.domains.practice.context import public_exercise
from app.application.agent.images import QuestionImageContext
from app.infrastructure.documents.question_images import QuestionImageReader, ImageReadError, ImageBudget


def question(identifier="math:ready", **kwargs):
    values = dict(id=identifier, course_id="math", source_id=identifier.split(":")[-1], stem="测试题" + identifier, options={}, answer=["私有答案"],
        explanation="私有解析", question_type="fill", concept_ids=[], generated=False, source_ref="fixture", version=1, validation={})
    values.update(kwargs)
    return Question(**values)


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    @contextmanager
    def session():
        with Session(engine, expire_on_commit=False) as db:
            with db.begin():
                yield db
    monkeypatch.setattr("app.domains.courses.catalog.db_session", session)
    monkeypatch.setattr("app.domains.courses.retrieval.service.db_session", session)
    monkeypatch.setattr("app.domains.courses.retrieval.indexing.db_session", session)
    with session() as db:
        db.add(Course(id="math", title="测试数学", description="", source_ref="fixture"))
        for name, validation in {"ready": {}, "reference": {"availability": "reference_only"},
            "missing": {"availability": "missing_figure"}, "withdrawn": {"visibility": "private"},
            "incomplete": {"availability": "source_incomplete"}, "unknown": {"availability": "invented"},
            "null": {"availability": None, "visibility": None}}.items():
            db.add(question("math:" + name, validation=validation))
        db.add(question("math:generated", generated=True))
    return session


@pytest.mark.parametrize("status,visible,generated,expected", [
    ("ready", "public", False, (True, True, True)),
    ("missing_figure", "public", False, (True, True, False)),
    ("reference_only", "public", False, (True, True, False)),
    ("source_incomplete", "public", False, (False, False, False)),
    ("ready", "private", False, (False, False, False)),
    ("ready", "public", True, (True, False, True)),
    ("invented", "public", False, (False, False, False)),
])
def test_question_capabilities_are_distinct(status, visible, generated, expected):
    rights = access(question(validation={"availability": status, "visibility": visible}, generated=generated))
    assert (rights.public_readable, rights.searchable, rights.independent_practice) == expected
    assert not rights.solution_readable


def test_missing_image_cannot_override_withdrawal_or_grant_solution_permission():
    q = question(stem="如图计算", validation={"availability": "source_incomplete", "solution_authorized": True})
    assert public_question(q)["availability"] == "source_incomplete" and not can_search(q)
    with pytest.raises(ValueError):
        require_access(q, "solution")
    assert access(q, solution_authorized=True, allow_archived_solution=True).solution_readable


def test_reference_assets_do_not_turn_reference_question_into_graded_practice():
    q = question("math:reference", validation={"availability": "reference_only"}, stem="如图计算")
    assets = [{"id": "a" * 64, "role": "stem", "status": "ready", "source": {}}]
    projected = public_question(q, assets=assets)
    assert projected["images"] and projected["availability"] == "reference_only"
    assert not access(q, assets=assets).independent_practice


def test_python_sql_and_offline_projection_have_the_same_searchable_set(database):
    with database() as db:
        rows = db.scalars(select(Question)).all()
        expected = {q.id for q in rows if can_search(q)}
        actual = set(db.scalars(select(Question.id).where(*searchable_conditions(Question))))
    projected = {q["id"] for q in indexing.snapshot("math")["questions"]}
    assert expected == actual == projected == {"math:ready", "math:reference", "math:missing", "math:null"}


def test_archived_question_internal_read_preserves_frozen_practice(database):
    with database() as db:
        q = db.get(Question, "math:ready")
        frozen = question_contract(q)
        q.validation = {"visibility": "private", "availability": "source_incomplete"}
    with pytest.raises(ValueError):
        CatalogService().question("math:ready", "math")
    internal = CatalogService().question("math:ready", "math", internal=True)
    assert internal["answer"] == frozen["answer"] == ["私有答案"]
    restored = public_exercise(frozen, "math")
    assert restored["stem"] == frozen["stem"] and "answer" not in restored and "explanation" not in restored


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_both_retrieval_routes_enforce_the_shared_policy(database, failed):
    with database() as db:
        hits = [{"_id": q.id, "_score": 1, "_source": {"question_id": q.id, "course_id": q.course_id, "text": q.stem, "version": 1}} for q in db.scalars(select(Question))]
    class Search:
        QUESTIONS = "questions"
        MATERIALS = "materials"
        async def request(self, *args, **kwargs):
            if failed:
                raise ConnectionError("unavailable")
            return {"hits": {"hits": hits}}
    settings = SimpleNamespace(exercise_search_courses="math", exercise_candidates=30, exercise_mm_enabled=False,
        exercise_rerank_enabled=False, exercise_mm_model="qwen3-vl-embedding", exercise_mm_dim=1024, exercise_mm_url="unused")
    models = SimpleNamespace(settings=settings)
    search = Search()
    legacy = RetrievalService(models, search, None)
    modern = QuestionRetrievalService(models, search, store=QuestionSearchStore(database))
    old = await legacy.retrieve("", course_id="math", kind="questions", mode="keyword", top_k=20)
    new = await modern.retrieve("", course_id="math", mode="keyword", top_k=20)
    expected = {"math:ready", "math:reference", "math:missing", "math:null"}
    assert {i["id"] for i in old["items"]} == {i["id"] for i in new["items"]} == expected
    assert old["degraded"] == new["degraded"] == failed


@pytest.mark.asyncio
async def test_channel_failure_keeps_successful_hits_but_cancellation_propagates():
    async def good():
        return {"hits": {"hits": [{"_id": "q"}]}}
    async def bad():
        raise ConnectionError()
    errors, timings = [], {}
    hits = await run_channels({"keyword": good, "vector": bad}, timings, errors)
    assert list(hits) == ["keyword"] and errors == ["vector:ConnectionError"]
    async def cancelled():
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await run_channels({"keyword": cancelled}, {}, [])


def test_linked_materials_are_not_collapsed_as_one_question():
    hits = [{"_id": name, "_source": {"question_id": "same"}} for name in ("material-b", "material-a")]
    assert len(fuse({"keyword": hits})) == 1
    assert [r["id"] for r in fuse({"keyword": hits}, by_question=False, stable_order=True)] == ["material-b", "material-a"]


def test_corrupt_png_is_rejected_by_shared_reader_and_tutoring_context(tmp_path):
    damaged = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jP1kAAAAASUVORK5CYII=")
    path = tmp_path / "question_1-image_0.png"
    path.write_bytes(damaged)
    with pytest.raises(ImageReadError):
        QuestionImageReader([tmp_path]).read(path)
    q = {"id": "math:1", "course_id": "math", "stem": "![题图](/api/assets/math/question_1-image_0.png)", "options": {}}
    assert all(p["type"] != "image_url" for p in QuestionImageContext("math", tmp_path).parts(q))


def test_each_consumer_can_have_its_own_budget():
    teaching = ImageBudget(16, 12)
    retrieval = ImageBudget(1, 12)
    image = {"bytes": 1}
    for _ in range(2):
        teaching.add(image)
    retrieval.add(image)
    with pytest.raises(ImageReadError):
        retrieval.add(image)
    assert teaching.count == 2 and retrieval.count == 1
