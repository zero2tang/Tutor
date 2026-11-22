"""统一记忆流水线的水位、关闭、退避与单队列；原 v1 自由文本候选已退役。"""

import asyncio
import json
from datetime import timedelta
import pytest
from sqlalchemy import select
from test_concept_memory import database, Model, add_fact
from app.domains.memory.service import MemoryService
from app.domains.memory.recording.jobs import MemoryWorker
from app.domains.memory.models import MemoryFact, MemoryJob
from app.domains.memory.scopes.models import SessionMemoryExtraction
from app.domains.sessions.models import LearningSession
from app.infrastructure.persistence.defaults import now


def test_same_watermark_does_not_reextract(database):
    add_fact(database,"s1","请先解释条件")
    model=Model()
    service=MemoryService(model)
    first=asyncio.run(service.refresh("s1"))
    asyncio.run(service.refresh("s1"))
    assert len(model.calls)==1 and first["session_experience"]["content"]
    with database() as db:
        assert len(list(db.scalars(select(SessionMemoryExtraction))))==1
        assert db.get(SessionMemoryExtraction,"s1").covered_seq==1


def test_threshold_and_active_turn_do_not_call_model(database):
    add_fact(database,"s1","本轮还未完成")
    model=Model();service=MemoryService(model)
    with database() as db:
        db.get(LearningSession,"s1").active_turn_id="active"
    result=service.capture("s1",force=True)
    assert result["job_id"] is None and not model.calls


def test_reopen_invalidates_old_final_watermark(database):
    add_fact(database,"s1","本次先到这里")
    service=MemoryService(Model())
    old=service.capture("s1",close=True)
    add_fact(database,"s1","继续上一轮","reopened")
    asyncio.run(MemoryWorker(service).run_once(job_id=old["job_id"]))
    with database() as db:
        assert db.get(MemoryJob,old["job_id"]).status=="superseded"
        assert not db.get(SessionMemoryExtraction,"s1").closed
        assert db.get(MemoryFact,db.scalar(select(MemoryFact.id).where(MemoryFact.source_ref=="message:reopened")))


def test_active_turn_defers_without_spending_retry_attempts(database):
    add_fact(database,"s1","测试等待")
    model=Model();service=MemoryService(model);worker=MemoryWorker(service)
    queued=service.capture("s1",force=True)
    with database() as db:
        db.get(LearningSession,"s1").active_turn_id="active"
    for _ in range(4):
        with database() as db:
            db.get(MemoryJob,queued["job_id"]).available_at=now()
        asyncio.run(worker.run_once(job_id=queued["job_id"]))
    with database() as db:
        row=db.get(MemoryJob,queued["job_id"])
        assert row.attempts==0 and row.status=="pending" and row.metrics["deferrals"]==4
    assert not model.calls


def test_model_failure_keeps_raw_fact_and_safe_retry(database):
    add_fact(database,"s1","原答必须保留")
    def failure(data):
        raise TimeoutError("PRIVATE_UPSTREAM")
    model=Model(failure);service=MemoryService(model)
    result=asyncio.run(service.refresh("s1"))
    row=result["jobs"][0]
    assert row["status"]=="retry" and row["error"]=="TimeoutError"
    assert "PRIVATE_UPSTREAM" not in json.dumps(result)
    with database() as db:
        assert list(db.scalars(select(MemoryFact)))


def test_expired_job_cannot_commit_model_result(database):
    add_fact(database,"s1","过期结果不能发布")
    def expire(data):
        with database() as db:
            row=db.scalar(select(MemoryJob).where(MemoryJob.status=="running"))
            row.lease_until=now()-timedelta(seconds=1)
        return {"observations":[],"preferences":[],"experience":"旧结果","experience_source_refs":[data["facts"][0]["source_ref"]]}
    service=MemoryService(Model(expire))
    result=asyncio.run(service.refresh("s1"))
    assert result["jobs"][0]["status"]=="retry"
    with database() as db:
        assert db.get(SessionMemoryExtraction,"s1").content==""


def test_startup_recovers_only_existing_work(database):
    service=MemoryService(Model())
    add_fact(database,"s1","已排队任务")
    queued=service.capture("s1",force=True)
    with database() as db:
        row=db.get(MemoryJob,queued["job_id"])
        row.status="running";row.lease_until=now()+timedelta(minutes=1)
    service.recover()
    with database() as db:
        assert db.get(MemoryJob,queued["job_id"]).status=="retry"
        assert db.get(LearningSession,"s2") and db.get(SessionMemoryExtraction,"s2") is None


def test_failed_old_worker_does_not_override_reclaimed_job(database):
    add_fact(database,"s1","旧工作者不能覆盖新租约")
    def reclaim(data):
        with database() as db:
            row=db.scalar(select(MemoryJob).where(MemoryJob.status=="running"))
            row.lease_token="new-worker"
        raise TimeoutError("旧请求失败")
    service=MemoryService(Model(reclaim))
    result=asyncio.run(service.refresh("s1"))
    assert result["jobs"][0]["status"]=="running"
    with database() as db:
        row=db.get(MemoryJob,result["jobs"][0]["id"])
        assert row.lease_token=="new-worker" and row.error is None
        assert not db.get(SessionMemoryExtraction,"s1").content
