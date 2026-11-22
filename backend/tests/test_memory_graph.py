"""记忆图验证可恢复候选、持久重试额度和提交后的可靠上层唤醒。"""

import asyncio
from datetime import timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlalchemy import select

from app.application.memory.graph import MemoryGraphRunner
from app.domains.memory.workflow import MemoryRuntime
from app.domains.memory.models import MemoryJob
from app.domains.memory.recording.jobs import MemoryWorker
from app.domains.memory.scopes.models import ScopeMemory, SessionMemoryExtraction
from app.domains.memory.scopes.service import HierarchicalMemoryService
from app.domains.memory.service import MemoryService
from app.infrastructure.persistence.defaults import now
from test_concept_memory import database, Model, add_fact, response_for
from test_hierarchical_memory import database as hierarchy_database, add_preference, ScopeModel


def test_leaf_candidate_survives_saver_reopen_without_model_repeat(database, tmp_path):
    add_fact(database, "s1", "希望以后先解释条件")
    first_model = Model()

    async def scenario():
        path = str(tmp_path / "memory-checkpoints.sqlite")
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            service = MemoryService(first_model)
            service.configure_graph(saver)

            def broken_publish(*args):
                raise TimeoutError("模拟候选检查点之后、业务提交之前中断")

            service.business_pipeline.publish_extraction = broken_publish
            result = await service.refresh("s1")
            job_id = result["job_id"]
            state = await service.memory_graph.graph.aget_state({"configurable": {"thread_id": "memory:" + job_id}})
            assert state.next == ("publish",)
            assert state.values["candidate"]["experience"]["content"]
        second_model = Model(lambda data: pytest.fail("已持久候选不能再次调用提取模型"))
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            service = MemoryService(second_model)
            service.configure_graph(saver)
            with database() as db:
                db.get(MemoryJob, job_id).available_at = now()
            await MemoryWorker(service).run_once(job_id=job_id)
            with database() as db:
                assert db.get(MemoryJob, job_id).status == "completed"
                assert db.get(SessionMemoryExtraction, "s1").content
            assert not second_model.calls
        return job_id

    asyncio.run(scenario())
    assert len(first_model.calls) == 1


def test_retry_budget_survives_runner_recreation(database):
    add_fact(database, "s1", "模型失败也不能丢掉已提交事实")

    def fail(data):
        raise TimeoutError("上游暂时不可用")

    model, saver = Model(fail), InMemorySaver()
    service = MemoryService(model)
    queued = service.capture("s1", force=True)
    for _ in range(6):
        service = MemoryService(model)
        service.configure_graph(saver)
        with database() as db:
            db.get(MemoryJob, queued["job_id"]).available_at = now()
        asyncio.run(MemoryWorker(service).run_once(job_id=queued["job_id"]))
    with database() as db:
        job = db.get(MemoryJob, queued["job_id"])
        assert job.attempts == 5 and job.status == "failed"
        assert not db.get(SessionMemoryExtraction, "s1").content
    assert len(model.calls) == 5


def test_last_claim_crash_cannot_gain_a_sixth_attempt(database):
    add_fact(database, "s1", "第五次尝试崩溃也必须守住持久上限")
    model = Model()
    first = MemoryService(model)
    queued = first.capture("s1", force=True)
    with database() as db:
        job = db.get(MemoryJob, queued["job_id"])
        job.status, job.attempts = "running", 5
    recovered = MemoryService(model)
    recovered.recover()
    asyncio.run(MemoryWorker(recovered).run_once(job_id=queued["job_id"]))
    with database() as db:
        job = db.get(MemoryJob, queued["job_id"])
        assert job.attempts == 5 and job.status == "failed"
    assert not model.calls


def test_last_concept_claim_crash_cannot_gain_a_sixth_attempt(database):
    from app.domains.memory.scopes.dependencies import ensure_scope_state
    from app.domains.memory.concepts.observations import RULE_VERSION

    model = Model()
    service = MemoryService(model)
    with database() as db:
        scope = ensure_scope_state(db, "alice", "concept", "math", "c1")
        job = MemoryJob(job_kind="scope_aggregate", scope_key=scope.scope_key,
            source_generation=scope.generation, rule_version=RULE_VERSION,
            status="running", attempts=5, input_fingerprint="")
        db.add(job)
        db.flush()
        job_id = job.id
    recovered = MemoryService(model)
    recovered.recover()
    asyncio.run(MemoryWorker(recovered).run_once(job_id=job_id))
    with database() as db:
        job = db.get(MemoryJob, job_id)
        assert job.attempts == 5 and job.status == "failed"
    assert not model.calls


def test_review_failure_resumes_review_without_regenerating_candidate(hierarchy_database):
    add_preference(hierarchy_database)
    generator = ScopeModel()

    class FailingReview(ScopeModel):
        async def chat(self, messages, **kwargs):
            raise TimeoutError("复核暂时不可用")

    async def scenario():
        saver = InMemorySaver()
        first = HierarchicalMemoryService(generator, hierarchy_database, review_models=FailingReview())
        first._memory_graph = MemoryGraphRunner(hierarchy=first, checkpointer=saver)
        with pytest.raises(TimeoutError):
            await first.aggregate("m1")
        with hierarchy_database() as db:
            job = db.scalar(select(MemoryJob).where(MemoryJob.status == "retry"))
            job.available_at = now()
        second = HierarchicalMemoryService(generator, hierarchy_database, review_models=ScopeModel())
        second._memory_graph = MemoryGraphRunner(hierarchy=second, checkpointer=saver)
        await second.aggregate("m1")
        assert second.read("m1")["course_memory"]["content"]
        with hierarchy_database() as db:
            completed = db.scalar(select(MemoryJob).where(MemoryJob.status == "completed"))
            assert completed.metrics["model_calls"] == 3

    asyncio.run(scenario())
    assert len(generator.calls) == 1


def test_leaf_commit_registers_scope_work_before_followup(database):
    add_fact(database, "s1", "提示后我改成2x+2")
    service = MemoryService(Model(response_for()))

    async def broken_followup(*args):
        raise RuntimeError("模拟叶子提交之后、即时聚合之前中断")

    service.business_pipeline.followup_extraction = broken_followup
    result = asyncio.run(service.refresh("s1"))
    with database() as db:
        assert db.get(MemoryJob, result["job_id"]).status == "completed"
        child = db.scalar(select(MemoryJob).where(MemoryJob.job_kind == "scope_aggregate", MemoryJob.status == "pending"))
        assert child is not None
        child_id = child.id
    fresh = MemoryService(Model(response_for()))
    asyncio.run(MemoryWorker(fresh).run_once(job_id=child_id))
    with database() as db:
        child = db.get(MemoryJob, child_id)
        assert child.status == "completed"
        assert db.get(ScopeMemory, child.scope_key).content
        assert "claims" not in child.result_snapshot


def test_store_retry_uses_framework_without_repeating_model_or_job_budget(database, monkeypatch):
    from app.domains.memory import document_staging
    from app.infrastructure.persistence.memory_documents import memory_document_repository

    add_fact(database, "s1", "先解释条件再继续")
    with database() as db:
        store = memory_document_repository(db.get_bind()).store
    original, calls = document_staging.stage_memory_fields, []

    async def transient(store, namespace, fields, **kwargs):
        calls.append(namespace)
        if len(calls) == 1:
            raise TimeoutError("首次Store写入暂时不可用")
        return await original(store, namespace, fields, **kwargs)

    monkeypatch.setattr(document_staging, "stage_memory_fields", transient)
    model = Model()
    service = MemoryService(model)
    service.configure_graph(InMemorySaver(), store=store)
    result = asyncio.run(service.refresh("s1"))
    with database() as db:
        job = db.get(MemoryJob, result["job_id"])
        assert job.status == "completed" and job.attempts == 1
        assert db.get(SessionMemoryExtraction, "s1").document_key
    assert len(calls) == 2 and len(model.calls) == 1


def test_replaying_committed_publication_does_not_increment_versions(database):
    add_fact(database, "s1", "已提交的经历不能重复写入")
    model = Model()
    service = MemoryService(model)
    saver = InMemorySaver()
    service.configure_graph(saver)
    original = service.business_pipeline.publish_extraction

    def commit_then_fail(*args):
        original(*args)
        raise TimeoutError("模拟SQL提交之后保存下一检查点之前中断")

    service.business_pipeline.publish_extraction = commit_then_fail

    async def scenario():
        result = await service.refresh("s1")
        config = {"configurable": {"thread_id": "memory:" + result["job_id"]}}
        state = await service.memory_graph.graph.aget_state(config)
        assert state.next == ("publish",)
        with database() as db:
            before = db.get(SessionMemoryExtraction, "s1").version
        service.business_pipeline.publish_extraction = original
        await service.memory_graph.run("extract", state.values["snapshot"])
        with database() as db:
            assert db.get(SessionMemoryExtraction, "s1").version == before

    asyncio.run(scenario())
    assert len(model.calls) == 1


def test_stop_before_validation_resumes_candidate_and_preserves_usage(database):
    add_fact(database, "s1", "候选已经保存，校验尚未开始")
    model, saver = Model(), InMemorySaver()
    service = MemoryService(model)
    service.configure_graph(saver)
    queued = service.capture("s1", force=True)
    with database() as db:
        job = db.get(MemoryJob, queued["job_id"])
        job.status, job.attempts, job.lease_token = "running", 1, "test-lease"
        job.lease_until = now() + timedelta(minutes=5)
    snapshot = service.business_pipeline._snapshot(queued["job_id"])

    async def scenario():
        runner = service.memory_graph
        config = {"configurable": {"thread_id": "memory:" + queued["job_id"]}}
        await runner.graph.ainvoke({"job_id": queued["job_id"], "kind": "extract", "snapshot": snapshot,
            "candidate": None, "validated": None, "audit": {}, "metrics": {}, "outcome": {}},
            config=config, context=MemoryRuntime(runner, "test-lease"),
            interrupt_before=["validate"], durability="sync")
        state = await runner.graph.aget_state(config)
        assert state.next == ("validate",) and not any(task.error for task in state.tasks)
        replacement = MemoryService(model)
        replacement.configure_graph(saver)
        await replacement.memory_graph.run("extract", snapshot)

    asyncio.run(scenario())
    with database() as db:
        job = db.get(MemoryJob, queued["job_id"])
        assert job.status == "completed" and job.metrics["model_calls"] == 1
    assert len(model.calls) == 1
