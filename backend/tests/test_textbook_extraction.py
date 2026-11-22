"""验证来源、恢复、发布原子性和原文答案边界；不以测试代替原页复核。"""

from contextlib import contextmanager
from types import SimpleNamespace
import json
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.infrastructure.persistence.database import Base
from app.domains.materials.extraction.store import ExtractionStore
from app.domains.materials.extraction.schemas import Region, PageExtraction
from app.domains.materials.extraction.models import ExtractedSection
from app.domains.courses.models import Question
from app.infrastructure.documents.pdf import digest
from app.application.textbooks.model_calls import structured_call
from app.application.textbooks.assembly import assemble, validate_design


@pytest.fixture
def store(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    @contextmanager
    def session():
        with Session(engine, expire_on_commit=False) as db:
            with db.begin():
                yield db
    return ExtractionStore(session)


def registered(store):
    manifest = {"source_hash": "a" * 64, "page_count": 12, "sections": []}
    book = store.register(manifest, ".", b"pdf", "test_book", "测试教材")
    section = {"number": "1.1", "title": "正数和负数", "page_start": 9, "page_end": 12}
    job = store.create_job(book, section, "version-a")
    return book, section, job


@pytest.mark.parametrize("bbox", [[0, 0, 0, 10], [0, -1, 10, 100], [2, 2, 1, 3], [0, 0, 1001, 2]])
def test_source_region_must_be_inside_page(bbox):
    with pytest.raises(ValueError):
        Region(bbox=bbox)


def test_job_lease_blocks_parallel_writer_and_failure_can_resume(store):
    _, _, job = registered(store)
    token = store.claim(job)
    with pytest.raises(ValueError, match="租约"):
        store.claim(job)
    store.fail(job, token, "临时故障")
    assert store.claim(job) != token


@pytest.mark.asyncio
async def test_successful_real_call_receipt_is_reused_but_invalid_json_is_not(store):
    _, _, job = registered(store)
    class FakeModel:
        settings = SimpleNamespace(llm_model="test")
        count = 0
        async def chat(self, *args, **kwargs):
            self.count += 1
            value = '{"printed_page":"2"}' if self.count > 1 else '{broken'
            return {"choices": [{"message": {"content": value}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10}}
    model = FakeModel()
    result = await structured_call(model, store, job, "page", 9, [], PageExtraction, 8000)
    assert result["printed_page"] == "2"
    await structured_call(model, store, job, "page", 9, [], PageExtraction, 8000)
    assert model.count == 2  # 格式修复回执同时映射原请求指纹，恢复时不重复付费。
    assert [call.status for call in store.calls(job)] == ["failed", "completed", "completed"]


def test_missing_answer_stays_null_and_commit_is_idempotent(store):
    _, section, job = registered(store)
    token = store.claim(job)
    payload = {**section, "definitions": [], "methods": [], "exercises": [
        {"source_id": "p010e01", "number": "1", "stem": "写出相反意义的量", "answer": None, "solution": None, "source_refs": [{"pdf_page": 10}]}],
        "teaching_design": {"summary": "从生活情境引入", "steps": []}}
    store.commit(job, token, payload, {})
    assert store.claim(job) is None
    with store.session() as db:
        assert db.scalar(select(Question)) is None
    store.review(job, {"payload_hash": digest(payload), "reviewer": "test-source-review", "passed": True})
    with store.session() as db:
        question = db.scalar(select(Question))
        assert question.answer is None and question.explanation == ""
        assert db.get(ExtractedSection, job).payload == payload
    assert store.section(job).payload_hash == digest(payload)
    with pytest.raises(ValueError, match="指纹"):
        store.review(job, {"payload_hash": "wrong", "reviewer": "test", "passed": True})


def test_invalid_lease_cannot_publish_partial_resources(store):
    _, _, job = registered(store)
    store.claim(job)
    with pytest.raises(ValueError, match="租约"):
        store.commit(job, "stale-token", {}, {})
    with store.session() as db:
        assert not db.get(ExtractedSection, job).payload_hash


def test_teaching_inference_cannot_cite_invented_sources():
    payload = {"pages": [{"definitions": [{"source_id": "d1", "source_refs": [{"pdf_page": 9}]}], "teaching_blocks": [], "methods": [], "exercises": []}]}
    design = {"steps": [{"source_ids": ["unknown"]}], "key_questions": [], "potential_difficulties": [], "methods": []}
    with pytest.raises(ValueError, match="来源"):
        validate_design(design, payload)


def test_cross_page_orphan_is_not_bound_to_another_question():
    page = {"pdf_page": 10, "definitions": [], "methods": [], "figures": [], "issues": [],
            "exercises": [{"continued_from_previous": True, "number": "2"}]}
    with pytest.raises(ValueError, match="跨页"):
        assemble({}, [page])


def test_source_review_revision_preserves_original_model_snapshot(store):
    _, section, job = registered(store)
    token = store.claim(job)
    payload = {**section, "definitions": [{"source_id": "d1", "name": "负数", "text": "漏字", "source_refs": [{"pdf_page": 9}]}],
               "exercises": [], "methods": [], "provenance": {"extractor_version": "test"},
               "teaching_design": {"summary": "概括", "steps": []}}
    store.commit(job, token, payload, {})
    patch = {"path": ["definitions", 0, "text"], "before": "漏字", "value": "核对原页后的文字", "source_refs": [{"pdf_page": 9}]}
    revision = store.amend(job, [patch], "source-review")
    assert store.section(job).payload["definitions"][0]["text"] == "漏字"
    assert store.section(revision).payload["definitions"][0]["text"] == "核对原页后的文字"
    assert store.amend(job, [patch], "source-review") == revision
    with pytest.raises(ValueError, match="原值"):
        store.amend(revision, [patch], "source-review")
    with pytest.raises(ValueError, match="来源"):
        store.amend(job, [{**patch, "source_refs": [{"pdf_page": 100}]}], "source-review")


@pytest.mark.parametrize("continuation", ["例4（3）", "例题4", "4"])
def test_cross_page_example_labels_are_canonicalized_without_merging_new_questions(continuation):
    def page(number, question):
        return {"pdf_page": number, "definitions": [], "methods": [], "figures": [], "issues": [], "exercises": [question]}
    first = {"number": "例4", "kind": "example", "stem": "观察数列", "continued_from_previous": False,
             "subquestions": [], "tables": [], "figure_ids": [], "source_refs": [{"pdf_page": 50}], "solution": "解（1）"}
    second = {**first, "number": continuation, "stem": "", "continued_from_previous": True,
              "source_refs": [{"pdf_page": 51}], "solution": "解（2）"}
    result = assemble({}, [page(50, first), page(51, second)])
    assert len(result["exercises"]) == 1
    assert len(result["exercises"][0]["source_refs"]) == 2
