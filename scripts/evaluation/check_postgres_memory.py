"""在独立临时数据库验收真实 PostgreSQL、三级记忆与模型链路。"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def save(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


async def exercise(report):
    """只发送自编学习素材，所有业务写入都在本次临时数据库。"""
    from sqlalchemy import select, func
    from app.infrastructure.config import get_settings
    from app.infrastructure.persistence.database import engine, db_session
    from app.application.learning.storage import checkpoint_storage
    from app.infrastructure.persistence.memory_documents import memory_document_storage
    from app.infrastructure.clients.llm import ModelClient
    from app.domains.courses.models import Course, Concept
    from app.domains.sessions.models import LearningSession, Message
    from app.domains.assessment.models import Attempt
    from app.domains.memory.models import MemoryJob
    from app.domains.memory.scopes.models import ScopeMemory
    from app.domains.memory.service import MemoryService
    from app.domains.memory.scopes.service import HierarchicalMemoryService
    from app.domains.memory.concepts.service import BusinessMemoryPipeline
    from app.domains.memory.recording.jobs import MemoryWorker

    settings = get_settings()
    assert settings.database_name.startswith("tutor_acceptance_"), "禁止向正式数据库写验收素材"
    sessions = []
    with db_session() as db:
        for course, title in [("fixture_algebra", "合成代数课程"), ("fixture_geometry", "合成几何课程")]:
            db.add(Course(id=course, title=title, description="数据库迁移验收自编素材", source_ref="fixture://postgres-migration"))
            for index in range(2):
                concept = f"{course}_{index}"
                name = ("移项", "去括号") [index] if course.endswith("algebra") else ("三角形内角和", "角度相减")[index]
                db.add(Concept(id=concept, course_id=course, source_id=concept, name=name, summary="合成验收知识点"))
                sid = str(uuid4())
                sessions.append(sid)
                db.add(LearningSession(id=sid, student_id="synthetic_student", course_id=course,
                    title="自编迁移验收", goal=f"学习{name}", concept_ids=[concept]))
                statement = (f"我正在练习{name}。刚才我把运算顺序弄错，重新写出每一步以后才发现遗漏。"
                    "以后学习本课程，请先让我独立尝试，再给分步提示。这个要求仅适用于本课程。"
                    "此外，以后所有课程都请使用中文交流，这是我的长期偏好。")
                db.add(Message(id=str(uuid4()), session_id=sid, role="user", content=statement,
                    agent="student", metadata_json={"guidance_snapshot": {"problem": {
                        "stem": f"自编{name}任务{index+1}：说明每一步使用的规则。"}}}))

    models = ModelClient()
    try:
        async with checkpoint_storage() as saver, memory_document_storage(settings.postgres_dsn(), engine=engine()) as documents:
            memory = MemoryService(models)
            hierarchy = HierarchicalMemoryService(models, database=memory.database)
            BusinessMemoryPipeline(models, memory, hierarchy=hierarchy)
            memory.configure_graph(saver, store=documents.async_store)
            worker = MemoryWorker(memory)
            for sid in sessions:
                await memory.refresh(sid, wait=True)
            # 子层发布已登记父层任务，显式消费本次有限工作集。
            for _ in range(48):
                if not await worker.run_once():
                    break
            with db_session() as db:
                rows = list(db.scalars(select(ScopeMemory).where(ScopeMemory.student_id == "synthetic_student")))
                levels = {kind: [row for row in rows if row.scope_kind == kind and row.content]
                          for kind in ("concept", "course", "student")}
                jobs = list(db.scalars(select(MemoryJob)))
                report["levels"] = {kind: len(items) for kind, items in levels.items()}
                report["documents"] = [{"scope": row.scope_kind, "key": row.document_key,
                    "claims": len(row.claims), "version": row.version} for row in rows if row.content]
                report["jobs"] = [{"id": job.id, "kind": job.job_kind, "status": job.status,
                    "attempts": job.attempts, "error": job.error, "metrics": job.metrics} for job in jobs]
                assert all(levels.values()), "三级记忆未全部形成"
                assert all(row.document_key for row in rows if row.content), "正文没有正式 Store 发布指针"
                assert not [job for job in jobs if job.status in {"failed", "running"}], "存在失败或未完成任务"
                assert db.scalar(select(func.count()).select_from(Attempt)) == 0, "语义记忆不得创建正式作答证据"
                keys = {row.scope_key: row.document_key for row in rows if row.content}
                finished = next(job.id for job in jobs if job.job_kind == "session_extract" and job.status == "completed")
            snapshot = await memory.memory_graph.graph.aget_state({"configurable": {"thread_id": "memory:" + finished}})
            assert snapshot.values, "任务图未持久化实际执行状态"
            report["graph_checkpoint_present"] = True

        # 关闭并重建官方存储连接，验证读取的是持久文档。
        async with checkpoint_storage() as saver, memory_document_storage(settings.postgres_dsn(), engine=engine()) as documents:
            with db_session() as db:
                restored = {row.scope_key: row.document_key for row in db.scalars(select(ScopeMemory)) if row.content}
                assert restored == keys
            report["reopened_document_count"] = len(restored)
            report["restart_read_verified"] = True
            # 遗忘必须经过 SQL 治理入口，旧 Store 版本即使仍存在也不可见。
            hierarchy.forget(sessions[0], "concept", concept_id="fixture_algebra_0")
            value = hierarchy.read(sessions[0])
            assert all(not row.get("content") for row in value.get("concept_memories", [])
                       if row["concept_id"] == "fixture_algebra_0")
            report["forget_visibility_verified"] = True
            report["formal_evidence_created"] = False
        report["passed"] = True
    finally:
        await models.close()


def worker(args):
    from alembic import command
    from app.infrastructure.persistence.schema import alembic_config
    from app.infrastructure.config import get_settings
    from app.application.learning.storage import setup_checkpoint_storage
    from app.infrastructure.persistence.memory_documents import setup_memory_document_storage

    report = {"started_at": datetime.now(timezone.utc).isoformat(), "synthetic_only": True, "passed": False}
    try:
        command.upgrade(alembic_config(), "head")
        setup_memory_document_storage(get_settings().postgres_dsn())
        from app.infrastructure.persistence.async_runtime import selector_loop
        async def run():
            await setup_checkpoint_storage(get_settings().postgres_dsn())
            await exercise(report)
        with asyncio.Runner(loop_factory=selector_loop) as runner:
            runner.run(run())
        if args.with_compaction:
            subprocess.run([sys.executable, str(ROOT / "scripts/evaluation/check_native_compaction.py")],
                           cwd=ROOT, check=True)
            compaction = json.loads((ROOT / "data/reports/postgresql_memory_migration/real_compaction.json").read_text(encoding="utf-8"))
            assert compaction.get("passed"), "真实连续压缩验收失败"
            report["real_compaction_passed"] = True
    except BaseException as exc:
        report.update(error_type=type(exc).__name__, error=str(exc)[:1000])
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(args.report, report)


def main(args):
    if args.worker:
        return worker(args)
    import psycopg
    from psycopg import sql
    from app.infrastructure.config import get_settings

    if args.report.exists():
        args.report.with_name(args.report.stem + "_previous_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json").write_bytes(args.report.read_bytes())
    database = "tutor_acceptance_" + uuid4().hex[:16]
    admin_dsn = get_settings().database_url(database=False).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    env = dict(os.environ, TUTOR_DATABASE=database, PYTHONPATH=str(ROOT) + os.pathsep + str(ROOT / "backend"))
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--report", str(args.report)]
    if args.with_compaction:
        command.append("--with-compaction")
    completed = subprocess.run(command, env=env, cwd=ROOT)
    report = json.loads(args.report.read_text(encoding="utf-8")) if args.report.exists() else {}
    if completed.returncode == 0 and report.get("passed"):
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
        report["temporary_database_removed"] = True
    else:
        report["debug_database"] = database
    save(args.report, report)
    print(json.dumps({"passed": report.get("passed", False), "report": str(args.report)}, ensure_ascii=False))
    return completed.returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--with-compaction", action="store_true", help="同时验证真实模型连续摘要及 PostgreSQL saver 重开")
    parser.add_argument("--report", type=Path, default=ROOT / "data/reports/postgresql_memory_migration/real_memory.json")
    raise SystemExit(main(parser.parse_args()))
