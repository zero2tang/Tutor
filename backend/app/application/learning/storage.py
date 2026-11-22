"""官方 PostgreSQL Saver 生命周期；业务执行权与冷线程保留规则单独校验。"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import select
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.infrastructure.config import ROOT, get_settings
from app.infrastructure.persistence.database import db_session
from app.domains.sessions.models import LearningSession, Message
from app.domains.sessions.interactions import pending_interaction

log = logging.getLogger(__name__)
CHECKPOINT_SCHEMA = "langgraph_checkpoint"


def checkpoint_serde():
    from app.domains.learning.schemas import Plan, Lesson
    from app.domains.practice.schemas import ExerciseSelection, ExerciseDraft, Explanation
    from app.domains.guidance.schemas import GuidedTeachingOutput
    return JsonPlusSerializer(allowed_msgpack_modules=[Plan, Lesson, ExerciseSelection,
        ExerciseDraft, Explanation, GuidedTeachingOutput], pickle_fallback=False)


def connection_options(schema):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
        raise ValueError("检查点 schema 名称非法")
    return {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
            "options": "-c search_path=" + schema + ",public", "connect_timeout": 10}


async def setup_checkpoint_storage(dsn=None, *, schema=CHECKPOINT_SCHEMA):
    """显式维护入口创建 schema，并让官方 Saver 维护自己的表版本。"""
    dsn = dsn or get_settings().postgres_dsn()
    options = connection_options(schema)
    async with await AsyncConnection.connect(dsn, **options) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        await AsyncPostgresSaver(connection, serde=checkpoint_serde()).setup()


@asynccontextmanager
async def checkpoint_storage(dsn=None, *, schema=CHECKPOINT_SCHEMA, setup=False):
    """默认只验证结构；独立 PG 连接持有会话锁，退出或崩溃即释放。"""
    dsn = dsn or get_settings().postgres_dsn()
    options = connection_options(schema)
    if setup:
        await setup_checkpoint_storage(dsn, schema=schema)
    lock_key = int.from_bytes(hashlib.sha256(("tutor-single-worker:" + schema).encode()).digest()[:8], "big", signed=True)
    async with await AsyncConnection.connect(dsn, **options) as owner:
        row = await (await owner.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key,))).fetchone()
        if not row["acquired"]:
            raise RuntimeError("同一业务库已有教学 worker，拒绝重复取得恢复执行权")
        try:
            # search_path 不允许在 schema 缺失时悄悄回退到 public 的同名表。
            exists = await (await owner.execute("SELECT to_regclass(%s) AS name", (schema + ".checkpoint_migrations",))).fetchone()
            if exists["name"] is None:
                raise RuntimeError("PostgreSQL 检查点尚未初始化，请先执行维护 prepare 命令")
            version = await (await owner.execute("SELECT MAX(v) AS version FROM checkpoint_migrations")).fetchone()
            if version["version"] != len(AsyncPostgresSaver.MIGRATIONS) - 1:
                raise RuntimeError("PostgreSQL 检查点版本不匹配，请先执行维护 prepare 命令")
            async with AsyncConnectionPool(dsn, kwargs=options, min_size=1, max_size=4,
                                           open=False, timeout=10, reconnect_timeout=10) as pool:
                await pool.wait()
                saver = AsyncPostgresSaver(pool, serde=checkpoint_serde())
                # 仅为维护统计标明 schema；不改变官方读写协议。
                saver.tutor_schema = schema
                yield saver
        finally:
            await owner.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))


async def cleanup_cold_threads(graph, saver, *, database=db_session):
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    with database() as db:
        rows = list(db.scalars(select(LearningSession)))
        candidates = []
        for row in rows:
            if row.active_turn_id or row.updated_at >= cutoff:
                continue
            messages = list(db.scalars(select(Message).where(Message.session_id == row.id)))
            workspace = (row.pending_context or {}).get("guided_teaching", {})
            unfinished = any((m.metadata_json or {}).get("_native_request", {}).get("status") == "received" for m in messages)
            if pending_interaction(row, messages) or unfinished or (workspace and not workspace.get("closed")):
                continue
            candidates.append(row.id)
    removed = 0
    for identifier in candidates:
        config = {"configurable": {"thread_id": "learning:" + identifier}}
        try:
            state = await graph.aget_state(config)
            if not state.next:
                await saver.adelete_thread(config["configurable"]["thread_id"])
                removed += 1
        except Exception:
            log.exception("冷线程清理失败，保留正式业务数据")
    schema = getattr(saver, "tutor_schema", CHECKPOINT_SCHEMA)
    counts, sizes = {}, {}
    async with saver.conn.connection() as connection:
        rows = await (await connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename", (schema,))).fetchall()
        tables = [row["tablename"] for row in rows]
        for table in tables:
            qualified = sql.Identifier(schema, table)
            count = await (await connection.execute(sql.SQL("SELECT COUNT(*) AS count FROM {}").format(qualified))).fetchone()
            size = await (await connection.execute("SELECT pg_total_relation_size(%s) AS bytes", (schema + "." + table,))).fetchone()
            counts[table], sizes[table] = count["count"], size["bytes"]
    report = {"backend": "postgresql", "schema": schema, "candidates": len(candidates), "removed": removed,
              "tables": tables, "rows": counts, "bytes": sizes,
              "maintenance_warning": sum(sizes.values()) > 256 * 1024 * 1024,
              "cold_days": 30, "active_history_hard_limit": False, "physical_shrink_guaranteed": False}
    if report["maintenance_warning"]:
        log.warning("PostgreSQL 检查点超过维护提醒阈值；不删除活跃或未完成线程。")
    output = ROOT / "data/reports/native_checkpoint_storage.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
