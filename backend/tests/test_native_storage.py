"""真实 saver 生命周期：只删除无业务待办的冷 END 线程，禁止多 worker。"""

import asyncio
import os
import subprocess
import sys
from uuid import uuid4
from sqlalchemy import create_engine, text
from app.infrastructure.config import get_settings
from datetime import datetime, timedelta
from typing import TypedDict

import pytest
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from app.application.learning.storage import checkpoint_storage, cleanup_cold_threads
from app.domains.sessions.models import LearningSession, Message
from test_native_session_graph import session_database


@pytest.fixture
def pg_storage():
    if os.getenv("TUTOR_TEST_POSTGRES") != "1":
        pytest.skip("设置 TUTOR_TEST_POSTGRES=1 运行真实 PostgreSQL 存储回归")
    schema = "tutor_test_checkpoint_" + uuid4().hex
    settings = get_settings()
    yield settings.postgres_dsn(), schema
    # 只清理本用例随机生成的 schema；生产 schema 从不进入此清理路径。
    assert schema.startswith("tutor_test_checkpoint_") and schema.isidentifier()
    engine = create_engine(settings.database_url())
    try:
        with engine.begin() as connection:
            connection.execute(text('DROP SCHEMA IF EXISTS "' + schema + '" CASCADE'))
    finally:
        engine.dispose()


def run(coroutine):
    return asyncio.run(coroutine, loop_factory=asyncio.SelectorEventLoop)


class State(TypedDict):
    wait: bool


def test_only_idle_cold_end_threads_are_deleted(session_database, pg_storage):
    dsn, schema = pg_storage
    ids = ("cold", "warm", "owned", "received", "waiting", "pending")
    with session_database() as db:
        for identifier in ids:
            db.add(LearningSession(id=identifier, student_id="local", course_id="math", title=identifier,
                goal="核对冷线程", concept_ids=[], updated_at=datetime.now() - timedelta(days=31),
                active_turn_id="owner" if identifier == "owned" else None,
                pending_question_id="math:q" if identifier == "pending" else None))
        db.get(LearningSession, "warm").updated_at = datetime.now()
        db.add(Message(session_id="received", role="user", content="尚未完成的输入", citations=[],
            metadata_json={"_native_request": {"status": "received"}}))

    async def scenario():
        async with checkpoint_storage(dsn, schema=schema, setup=True) as saver:
            builder = StateGraph(State)

            def activity(state):
                if state["wait"]:
                    interrupt("等待输入")
                return state

            builder.add_node("activity", activity)
            builder.add_edge(START, "activity")
            builder.add_edge("activity", END)
            graph = builder.compile(checkpointer=saver)
            for identifier in ids:
                await graph.ainvoke({"wait": identifier == "waiting"},
                    {"configurable": {"thread_id": "learning:" + identifier}}, durability="sync")
            report = await cleanup_cold_threads(graph, saver, database=session_database)
            # 原 fixture session 也是新线程；只有 cold 是超过30天且无待办的 END。
            assert report["removed"] == 1
            assert {"checkpoints", "checkpoint_writes", "checkpoint_blobs", "checkpoint_migrations"} <= set(report["tables"])
            assert sum(report["bytes"].values()) > 0
            assert not report["active_history_hard_limit"]
            for identifier in ids:
                value = await saver.aget_tuple({"configurable": {"thread_id": "learning:" + identifier}})
                assert (value is None) == (identifier == "cold")

    run(scenario())


def test_postgres_excludes_second_worker_and_releases_owner(pg_storage):
    dsn, schema = pg_storage

    async def scenario():
        async with checkpoint_storage(dsn, schema=schema, setup=True):
            with pytest.raises(RuntimeError, match="已有教学 worker"):
                async with checkpoint_storage(dsn, schema=schema):
                    pytest.fail("同一个检查点 schema 不能允许第二个 worker 进入 recover")
        # 正常退出后锁释放，可重新启动。
        async with checkpoint_storage(dsn, schema=schema, setup=True) as saver:
            assert await saver.aget_tuple({"configurable": {"thread_id": "absent"}}) is None

    run(scenario())



def test_unprepared_schema_fails_without_creating_tables(pg_storage):
    dsn, schema = pg_storage

    async def scenario():
        with pytest.raises(RuntimeError, match="尚未初始化"):
            async with checkpoint_storage(dsn, schema=schema):
                pytest.fail("正式启动不能自动创建框架表")

    run(scenario())
    engine = create_engine(get_settings().database_url())
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT to_regnamespace(:schema)"), {"schema": schema}) is None
    finally:
        engine.dispose()


def test_checkpoint_survives_a_real_process_restart(pg_storage):
    dsn, schema = pg_storage
    code = r"""
import asyncio,json,os
from typing import TypedDict
from langgraph.graph import StateGraph,START,END
from app.application.learning.storage import checkpoint_storage
class State(TypedDict):
    count:int
async def scenario():
    async with checkpoint_storage(os.environ['TUTOR_TEST_PG_DSN'],schema=os.environ['TUTOR_TEST_PG_SCHEMA'],setup=os.environ['TUTOR_TEST_STEP']=='write') as saver:
        graph=StateGraph(State)
        graph.add_node('increment',lambda state:{'count':state['count']+1})
        graph.add_edge(START,'increment');graph.add_edge('increment',END)
        compiled=graph.compile(checkpointer=saver)
        config={'configurable':{'thread_id':'restart-proof'}}
        if os.environ['TUTOR_TEST_STEP']=='write':
            await compiled.ainvoke({'count':4},config,durability='sync')
        saved=await compiled.aget_state(config)
        assert saved.values['count']==5
        print(json.dumps({'count':saved.values['count'],'process':os.getpid()}))
asyncio.run(scenario(),loop_factory=asyncio.SelectorEventLoop)
"""
    env = {**os.environ, "TUTOR_TEST_PG_DSN": dsn, "TUTOR_TEST_PG_SCHEMA": schema}
    outputs = []
    for step in ("write", "read"):
        result = subprocess.run([sys.executable, "-c", code], env={**env, "TUTOR_TEST_STEP": step},
                                capture_output=True, text=True, timeout=45, check=True)
        outputs.append(__import__("json").loads(result.stdout.strip()))
    assert [item["count"] for item in outputs] == [5, 5]
    assert outputs[0]["process"] != outputs[1]["process"]
