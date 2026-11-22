"""显式启用的真实 PostgreSQL 验收；只使用本用例创建的临时数据库。"""

from dataclasses import dataclass, field
from copy import deepcopy
import asyncio
import os
from uuid import uuid4

import pytest
from psycopg import Connection, sql
from psycopg.conninfo import make_conninfo
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.infrastructure.config import get_settings
from app.infrastructure.persistence.async_runtime import selector_loop
from app.infrastructure.persistence.memory_documents import (
    MemoryDocumentError, document_namespace, memory_document_storage,
    memory_document_repository, setup_memory_document_storage, stage_memory_fields,
)
from app.domains.memory.scopes.models import ScopeMemory


pytestmark = pytest.mark.skipif(os.getenv("TUTOR_TEST_POSTGRES") != "1",
    reason="设置 TUTOR_TEST_POSTGRES=1 后才创建独立 PostgreSQL 测试库")


@dataclass
class PostgresSandbox:
    dsn: str = field(repr=False)
    engine: object


@pytest.fixture
def postgres_database():
    settings = get_settings()
    name = "tutor_store_test_" + uuid4().hex[:16]
    admin_dsn = make_conninfo(settings.postgres_dsn(), dbname="postgres")
    with Connection.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    engine = create_engine(settings.database_url().set(database=name))
    dsn = make_conninfo(settings.postgres_dsn(), dbname=name)
    try:
        ScopeMemory.__table__.create(engine)
        setup_memory_document_storage(dsn)
        yield PostgresSandbox(dsn, engine)
    finally:
        engine.dispose()
        # 库名由本fixture生成，不接收环境提供的删除目标。
        assert name.startswith("tutor_store_test_") and len(name) == len("tutor_store_test_") + 16
        with Connection.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def memory_row():
    return ScopeMemory(scope_key="student:alice", student_id="alice", scope_kind="student",
        course_id="", concept_id="", content="先提示", claims=[], source_refs=[])


def on_selector(test):
    """测试只更换自己的 Runner，不修改进程级 event-loop policy。"""
    def run(postgres_database):
        with asyncio.Runner(loop_factory=selector_loop) as runner:
            return runner.run(test(postgres_database))
    return run


@on_selector
async def test_postgres_sync_async_store_and_rollback(postgres_database):
    target = postgres_database
    async with memory_document_storage(target.dsn, engine=target.engine) as stores:
        with Session(target.engine, expire_on_commit=False) as db:
            row = memory_row()
            db.add(row)
            db.commit()
            first, namespace = row.document_key, document_namespace(row)
        item = await stores.async_store.aget(namespace, first)
        assert item.value["fields"]["content"] == "先提示"
        repository = memory_document_repository(target.engine)
        key = await stage_memory_fields(stores.async_store, namespace,
            {"content": "新版本", "claims": [], "source_refs": []}, repository=repository)
        assert stores.sync_store.get(namespace, key).value["fields"]["content"] == "新版本"
        with Session(target.engine, expire_on_commit=False) as db:
            row = db.get(ScopeMemory, "student:alice")
            row.content = "新版本"
            db.flush()
            assert row.document_key == key
            db.rollback()
            assert row.document_key == first and row.content == "先提示"
        with target.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM langgraph_store.store")) == 2
            assert connection.scalar(text("SELECT to_regclass('public.store')")) is None
            assert connection.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='vector'")) == 0


@on_selector
async def test_postgres_missing_and_corrupt_documents_fail_closed(postgres_database):
    target = postgres_database
    async with memory_document_storage(target.dsn, engine=target.engine) as stores:
        with Session(target.engine, expire_on_commit=False) as db:
            row = memory_row()
            db.add(row)
            db.commit()
            key, namespace = row.document_key, document_namespace(row)
        original = deepcopy((await stores.async_store.aget(namespace, key)).value)
        await stores.async_store.adelete(namespace, key)
        with Session(target.engine) as db, pytest.raises(MemoryDocumentError, match="不存在"):
            db.get(ScopeMemory, "student:alice")
        corrupted = deepcopy(original)
        corrupted["fields"]["content"] = "伪造"
        await stores.async_store.aput(namespace, key, corrupted)
        with Session(target.engine) as db, pytest.raises(MemoryDocumentError, match="哈希"):
            db.get(ScopeMemory, "student:alice")


@on_selector
async def test_postgres_cross_student_pointer_is_rejected(postgres_database):
    target = postgres_database
    async with memory_document_storage(target.dsn, engine=target.engine):
        with Session(target.engine) as db:
            db.add(memory_row())
            db.commit()
        # 模拟SQL中受损的归属，不能因持有有效revision key而读出另一学生正文。
        with target.engine.begin() as connection:
            connection.execute(text("UPDATE scope_memories SET student_id='bob' WHERE scope_key='student:alice'"))
        with Session(target.engine) as db, pytest.raises(MemoryDocumentError):
            db.get(ScopeMemory, "student:alice")


@pytest.mark.parametrize("damage", ["missing_schema", "missing_store", "old_version", "future_version"])
def test_postgres_store_rejects_unprepared_schema_without_ddl(postgres_database, damage):
    target = postgres_database
    with target.engine.begin() as connection:
        if damage == "missing_schema":
            connection.execute(text("DROP SCHEMA langgraph_store CASCADE"))
        elif damage == "missing_store":
            connection.execute(text("DROP TABLE langgraph_store.store"))
        elif damage == "old_version":
            connection.execute(text("DELETE FROM langgraph_store.store_migrations "
                                    "WHERE v=(SELECT max(v) FROM langgraph_store.store_migrations)"))
        else:
            connection.execute(text("INSERT INTO langgraph_store.store_migrations(v) "
                                    "SELECT max(v)+1 FROM langgraph_store.store_migrations"))

    async def start():
        async with memory_document_storage(target.dsn, engine=target.engine):
            pytest.fail("未准备好的 Store 不应进入 ready 状态")

    with asyncio.Runner(loop_factory=selector_loop) as runner:
        with pytest.raises(MemoryDocumentError, match="初始化|版本"):
            runner.run(start())
    with pytest.raises(MemoryDocumentError, match="尚未绑定"):
        memory_document_repository(target.engine)
    with target.engine.connect() as connection:
        if damage == "missing_schema":
            assert connection.scalar(text("SELECT to_regnamespace('langgraph_store')")) is None
        elif damage == "missing_store":
            assert connection.scalar(text("SELECT to_regclass('langgraph_store.store')")) is None
