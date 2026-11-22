"""验证 Store 文档发布、归属、嵌套修改与 SQL 回滚边界。"""

from copy import deepcopy

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.infrastructure.persistence.database import Base
from app.infrastructure.persistence.memory_documents import (
    MemoryDocumentError, document_namespace, memory_document_repository,
    stage_memory_fields, unbind_memory_document_store,
)
from app.domains.memory.scopes.models import ScopeMemory, SessionMemoryExtraction


@pytest.fixture
def storage(tmp_path):
    engine = create_engine("sqlite:///" + (tmp_path / "documents.db").as_posix())
    Base.metadata.create_all(engine)
    yield engine, memory_document_repository(engine)
    engine.dispose()


def scope(**changes):
    values = dict(scope_key="student:alice", student_id="alice", scope_kind="student",
                  course_id="", concept_id="", content="先提示", claims=[], source_refs=[])
    return ScopeMemory(**{**values, **changes})


def test_documents_have_no_duplicate_sql_body_and_survive_detach(storage):
    engine, repository = storage
    columns = {item["name"] for item in inspect(engine).get_columns("scope_memories")}
    assert "document_key" in columns and not {"content", "claims", "source_refs"} & columns
    with Session(engine, expire_on_commit=False) as db:
        row = scope(claims=[{"text": "先提示", "source_refs": ["message:1"]}])
        db.add(row)
        db.commit()
        key, namespace = row.document_key, document_namespace(row)
    assert repository.read(namespace, key)["content"] == "先提示"
    with Session(engine) as db:
        loaded = db.get(ScopeMemory, "student:alice")
    assert loaded.content == "先提示"


def test_nested_change_is_published_without_other_column_changes(storage):
    engine, repository = storage
    with Session(engine) as db:
        row = scope(claims=[{"source_refs": ["message:1"]}])
        db.add(row)
        db.commit()
        previous = row.document_key
        row.claims[0]["source_refs"].append("message:2")
        db.commit()
        assert row.document_key != previous
    with Session(engine) as db:
        assert db.get(ScopeMemory, "student:alice").claims[0]["source_refs"] == ["message:1", "message:2"]


def test_sql_rollback_keeps_prior_pointer_and_expires_document_cache(storage):
    engine, repository = storage
    with Session(engine, expire_on_commit=False) as db:
        row = scope()
        db.add(row)
        db.commit()
        previous = row.document_key
        row.content = "未提交修改"
        db.flush()
        orphan = row.document_key
        assert orphan != previous
        db.rollback()
        assert row.document_key == previous and row.content == "先提示"
        assert repository.read(document_namespace(row), orphan)["content"] == "未提交修改"
    with Session(engine) as db:
        assert db.get(ScopeMemory, "student:alice").document_key == previous


def test_expire_and_refresh_discard_unpublished_nested_changes(storage):
    engine, _ = storage
    with Session(engine, expire_on_commit=False) as db:
        row = scope(claims=[{"source_refs": ["message:1"]}])
        db.add(row)
        db.commit()
        row.claims[0]["source_refs"].append("message:2")
        db.expire(row)
        assert row.claims[0]["source_refs"] == ["message:1"]
        row.content = "未发布"
        db.refresh(row)
        assert row.content == "先提示"


def test_cross_owner_pointer_and_corrupt_revision_fail_closed(storage):
    engine, repository = storage
    with Session(engine, expire_on_commit=False) as db:
        row = scope()
        db.add(row)
        db.commit()
        key, namespace = row.document_key, document_namespace(row)
        row.student_id = "other"
        with pytest.raises(MemoryDocumentError):
            db.flush()
        db.rollback()
    value = deepcopy(repository.store.get(namespace, key).value)
    value["fields"]["content"] = "篡改正文"
    repository.store.put(namespace, key, value)
    with Session(engine) as db, pytest.raises(MemoryDocumentError, match="哈希"):
        db.get(ScopeMemory, "student:alice")


def test_store_failure_never_publishes_pointer(storage, monkeypatch):
    engine, repository = storage
    with Session(engine) as db:
        db.add(scope())
        db.commit()
    def failed(*args, **kwargs):
        raise OSError("store unavailable")
    monkeypatch.setattr(type(repository.store), "put", failed)
    with Session(engine) as db:
        db.get(ScopeMemory, "student:alice").content = "新内容"
        with pytest.raises(OSError):
            db.commit()
        db.rollback()
    with Session(engine) as db:
        assert db.get(ScopeMemory, "student:alice").content == "先提示"


@pytest.mark.asyncio
async def test_async_stage_is_not_public_until_sql_commit(storage, monkeypatch):
    engine, repository = storage
    with Session(engine, expire_on_commit=False) as db:
        row = scope()
        db.add(row)
        db.commit()
        previous = row.document_key
        fields = {"content": "异步候选", "claims": [], "source_refs": []}
        staged = await stage_memory_fields(repository.store, document_namespace(row), fields, repository=repository)
        assert row.document_key == previous
        monkeypatch.setattr(type(repository.store), "get", lambda *args: pytest.fail("预写缓存命中不应再次读 Store"))
        monkeypatch.setattr(type(repository.store), "put", lambda *args, **kwargs: pytest.fail("预写缓存命中不应再次写 Store"))
        row.content = fields["content"]
        db.flush()
        assert row.document_key == staged
        db.rollback()


def test_missing_binding_has_no_production_fallback(storage):
    engine, _ = storage
    unbind_memory_document_store(engine)
    with pytest.raises(MemoryDocumentError, match="尚未绑定"):
        memory_document_repository(engine)
