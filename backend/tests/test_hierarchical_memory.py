from app.domains.memory.models import MemoryJob
from app.domains.memory.scopes.models import SessionMemoryExtraction
from reference_helpers import put_scope
"""验证业务作用域、晋升门槛、短事务CAS、撤销传播与输入预算。"""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.infrastructure.persistence.database import Base
from app.domains.memory.scopes.invalidation import bump_scope, invalidate_dependents
from app.domains.memory.scopes.dependencies import ensure_scope_state, scope_key, allowed_sources
from app.domains.memory.scopes.service import HierarchicalMemoryService
from app.domains.memory.scopes.promotion import promotion_eligible
from app.domains.memory.schemas import estimate_tokens
from app.domains.memory.scopes.models import ScopeMemory, SessionMemoryExtraction
from app.domains.courses.models import Concept
from app.infrastructure.persistence.defaults import now
from app.domains.sessions.models import LearningSession
from app.domains.memory.models import MemoryFact


@pytest.fixture
def database(tmp_path):
    engine = create_engine("sqlite:///" + (tmp_path / "hierarchy.db").as_posix())
    Base.metadata.create_all(engine)

    @contextmanager
    def factory():
        with Session(engine, expire_on_commit=False) as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    with factory() as db:
        for sid, student, course in [("m1", "s", "math"), ("m2", "s", "math"),
                                     ("p1", "s", "physics"), ("p2", "s", "physics"),
                                     ("other", "someone", "math")]:
            db.add(LearningSession(id=sid, student_id=student, course_id=course,
                title="测试", goal="学习", concept_ids=[course + "-a", course + "-b"]))
        for course in ["math", "physics"]:
            for suffix in ["a", "b"]:
                db.add(Concept(id=course + "-" + suffix, course_id=course,
                    source_id=suffix, name=course + suffix))
    yield factory
    engine.dispose()


def add_concept(database, sid="m1", concept="math-a", refs=None, text="提示后能解释，仍需独立验证"):
    refs = refs or [sid + ":answer"]
    with database() as db:
        session = db.get(LearningSession, sid)
        for index, ref in enumerate(refs):
            db.add(MemoryFact(id=ref, session_id=sid, seq=index + 1, source_ref=ref,
                kind="message", payload={"role": "user", "content": "请以后先给提示再展开；我这次需要提示。",
                                        "canonical_item_id": "task-" + sid}))
        state = db.get(SessionMemoryExtraction, sid)
        if state is None:
            db.merge(SessionMemoryExtraction(session_id=sid, student_id=session.student_id, course_id=session.course_id, next_seq=len(refs)))
        else:
            state.next_seq = max(state.next_seq, len(refs))
        key = scope_key(session.student_id, "concept", session.course_id, concept)
        ensure_scope_state(db, session.student_id, "concept", session.course_id, concept)
        put_scope(db, scope_key=key, student_id=session.student_id, scope_kind="concept",
            course_id=session.course_id, concept_id=concept, content=text, version=1,
            claims=[{"claim_id": "claim-" + sid, "text": text, "source_refs": refs,
                "counter_source_refs": [], "concept_ids": [concept], "course_ids": [session.course_id],
                "task_ids": ["task-" + sid]}], source_refs=refs)
        parent = ensure_scope_state(db, session.student_id, "course", session.course_id)
        bump_scope(db, parent.scope_key)
    return key


def add_preference(database, sid="m1", scope="course", text="本课程以后请先给提示"):
    with database() as db:
        session = db.get(LearningSession, sid)
        ref = "pref:" + sid
        db.add(MemoryFact(id=ref, session_id=sid, seq=99, source_ref=ref, kind="message",
                         payload={"role": "user", "content": text}))
        extraction = db.get(SessionMemoryExtraction, sid)
        if extraction is None:
            extraction = SessionMemoryExtraction(session_id=sid, student_id=session.student_id,
                course_id=session.course_id)
            db.add(extraction)
        extraction.covered_seq, extraction.version, extraction.source_refs = 99, 1, [ref]
        extraction.preferences = [{"claim_id": "pref-" + sid, "scope_kind": scope, "text": text,
            "source_refs": [ref], "source_quotes": [{"source_ref": ref, "quote": text}], "explicit": True}]
    return ref


class ScopeModel:
    def __init__(self, hook=None, decision="keep", no_change=False):
        self.calls, self.hook, self.decision, self.no_change = [], hook, decision, no_change

    async def chat(self, messages, **kwargs):
        data = json.loads(messages[1]["content"])
        self.calls.append((deepcopy(data), kwargs))
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        if "candidates" in data:
            result = {"decisions": [{"claim_id": c["claim_id"], "decision": self.decision,
                                      "reason": "测试审查"} for c in data["candidates"]]}
        elif self.no_change:
            result = {"no_change": True}
        else:
            preference = next(iter(data["preferences"]), None)
            refs = preference["source_refs"] if preference else list(data["facts"])
            text = preference["text"] if preference else "不同任务中分步提示后能继续解释，需独立验证"
            result = {"no_change": False, "claims": [{"text": text, "source_refs": refs,
                "counter_source_refs": [], "explicit_preference": preference is not None,
                "preference_id": preference["claim_id"] if preference else None,
                "epistemic_status": "observed", "facet": "preference" if preference else "learning_strategy"}]}
        return {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 30}}


def test_course_requires_two_concepts_and_distinct_tasks(database):
    add_concept(database)
    model = ScopeModel()
    service = HierarchicalMemoryService(model, database)
    asyncio.run(service.aggregate("m1"))
    assert not service.read("m1")["course_memory"]["content"]
    assert len(model.calls) == 1  # 不合门槛的候选不调用晋升复核。
    add_concept(database, "m2", "math-b")
    asyncio.run(service.aggregate("m2"))
    assert service.read("m1")["course_memory"]["content"]
    assert not service.read("m1")["global_memory"]["content"]


def test_same_task_does_not_become_two_independent_events():
    facts = {"a": {"task_id": "same", "course_id": "math", "concept_ids": ["a"]},
             "b": {"task_id": "same", "course_id": "math", "concept_ids": ["b"]}}
    assert not promotion_eligible({"source_refs": ["a", "b"]}, "course", facts, [])
    facts["b"]["task_id"] = "another"
    assert promotion_eligible({"source_refs": ["a", "b"]}, "course", facts, [])
    assert not promotion_eligible({"source_refs": ["a", "b"]}, "student", facts, [])


def test_student_pattern_requires_distinct_courses(database):
    for sid, concept in [("m1", "math-a"), ("m2", "math-b"),
                         ("p1", "physics-a"), ("p2", "physics-b")]:
        add_concept(database, sid, concept)
    model = ScopeModel()
    service = HierarchicalMemoryService(model, database)
    asyncio.run(service.aggregate("m1"))
    assert not service.read("m1")["global_memory"]["content"]
    asyncio.run(service.aggregate("p1"))
    assert service.read("m1")["global_memory"]["content"]
    assert any("candidates" in data and data["scope_kind"] == "student" for data, _ in model.calls)


def test_course_preferences_isolated_and_global_preference_direct(database):
    add_preference(database, text="代数课程以后请先给提示")
    add_preference(database, "p1", text="物理课程以后请先画图")
    model = ScopeModel()
    service = HierarchicalMemoryService(model, database)
    asyncio.run(service.aggregate("m1"))
    asyncio.run(service.aggregate("p1"))
    assert "先给提示" in service.read("m1")["course_memory"]["content"]
    assert "先画图" in service.read("p1")["course_memory"]["content"]
    assert not service.read("p1")["global_memory"]["content"]


def test_explicit_global_preference_bypasses_multicourse_gate(database):
    add_preference(database, scope="student", text="以后所有课程都先给提示")
    model = ScopeModel()
    service = HierarchicalMemoryService(model, database)
    asyncio.run(service.aggregate("m1"))
    assert "所有课程" in service.read("m1")["global_memory"]["content"]
    assert sum("candidates" in d for d, _ in model.calls) == 1


@pytest.mark.parametrize("decision", ["downscope", "reject"])
def test_independent_review_cannot_invent_replacement(database, decision):
    add_preference(database)
    service = HierarchicalMemoryService(ScopeModel(decision=decision), database)
    asyncio.run(service.aggregate("m1"))
    assert not service.read("m1")["course_memory"]["content"]
    with database() as db:
        job = db.scalar(select(MemoryJob).where(MemoryJob.status == "completed"))
        assert job.result_snapshot["review"]["decisions"][0]["decision"] == decision


def test_processed_input_has_no_duplicate_model_charge(database):
    add_preference(database)
    model = ScopeModel()
    service = HierarchicalMemoryService(model, database)
    asyncio.run(service.aggregate("m1"))
    count = len(model.calls)
    asyncio.run(service.aggregate("m1"))
    assert len(model.calls) == count
    assert service.read("m1")["course_memory"]["version"] == 1
    with database() as db:
        extraction = db.get(SessionMemoryExtraction, "m1")
        extraction.version += 1
        extraction.covered_seq += 2  # 只有会话处理水位推进，偏好正文未变化。
    asyncio.run(service.aggregate("m1"))
    assert len(model.calls) == count


def test_cas_rejects_new_input_generation_and_preserves_audit(database):
    add_preference(database)

    def race():
        with database() as db:
            bump_scope(db, scope_key("s", "course", "math"))

    model = ScopeModel(hook=race)
    service = HierarchicalMemoryService(model, database)
    result = asyncio.run(service.aggregate("m1"))
    assert result["course"]["status"] == "superseded"
    assert not service.read("m1")["course_memory"]["content"]
    with database() as db:
        job = db.scalar(select(MemoryJob).where(MemoryJob.status == "superseded"))
        assert job.metrics["model_calls"] == 2


def test_ordinary_new_version_allows_frozen_context_forget_revokes(database):
    add_concept(database)
    service = HierarchicalMemoryService(ScopeModel(), database)
    context = service.context_for_session("m1")
    assert service.validate_context(context["memory_manifest"])
    with database() as db:
        row = db.get(ScopeMemory, scope_key("s", "concept", "math", "math-a"))
        row.version += 1
        row.content = "新观察"
    assert service.validate_context(context["memory_manifest"])
    service.forget("m1", "concept", "math-a")
    assert not service.validate_context(context["memory_manifest"])
    assert not service.context_for_session("m1")["concept_memories"]


def test_forget_propagates_only_actual_current_dependencies(database):
    child = add_concept(database)
    unrelated = add_concept(database, "p1", "physics-a")
    with database() as db:
        for kind, course, parent_child in [("course", "math", child), ("course", "physics", unrelated),
                                           ("student", "", scope_key("s", "course", "math"))]:
            state = ensure_scope_state(db, "s", kind, course)
            put_scope(db, scope_key=state.scope_key, student_id="s", scope_kind=kind,
                course_id=course, concept_id="", content="需要验证的策略", version=1,
                source_refs=["m1:answer"], claims=[])
            pass  # 固定三级失效不再登记通用依赖边。
    service = HierarchicalMemoryService(ScopeModel(), database)
    result = service.forget("m1", "concept", "math-a")
    assert scope_key("s", "course", "math") in result["affected_scope_keys"]
    assert scope_key("s", "student") in result["affected_scope_keys"]
    assert scope_key("s", "course", "physics") not in result["affected_scope_keys"]
    assert not service.read("m1")["global_memory"]["content"]
    assert service.read("p1")["course_memory"]["content"]


def test_tombstone_prevents_same_old_source_repromotion(database):
    add_preference(database)
    model = ScopeModel()
    service = HierarchicalMemoryService(model, database)
    asyncio.run(service.aggregate("m1"))
    service.forget("m1", "course")
    with database() as db:
        extraction = db.get(SessionMemoryExtraction, "m1")
        extraction.version += 1  # 模拟旧原文再次提取，来源仍是撤销前的同一条。
    asyncio.run(service.aggregate("m1"))
    assert not service.read("m1")["course_memory"]["content"]


def test_scope_budget_includes_metadata_and_filters_foreign_concepts(database):
    for sid, concept in [("m1", "math-a"), ("m2", "math-b"), ("p1", "physics-a")]:
        add_concept(database, sid, concept, text="需独立验证，帮助条件已记录。" * 24)
    service = HierarchicalMemoryService(ScopeModel(), database)
    value = service.context_for_session("m1", ["math-a", "physics-a", "math-b"], input_budget=9000)
    assert len(value["concept_memories"]) <= 2
    assert all(v["course_id"] == "math" for v in value["concept_memories"])
    assert value["memory_input_tokens"] <= value["memory_budget"] == 1800
    payload = service.context_for_session("m1", input_budget=100)["model_payload"]
    assert payload == {}
    grading = service.context_for_session("m1", purpose="grading")
    assert grading["global_memory"] is None and not grading["concept_memories"]


def test_correct_preserves_raw_facts_and_requires_extraction(database):
    add_concept(database)
    service = HierarchicalMemoryService(ScopeModel(), database)
    result = asyncio.run(service.correct("m1", "这不是持续困难，请保留为本次现象", "concept", "math-a"))
    assert result["requires_extraction"] is True
    with database() as db:
        original = db.get(MemoryFact, "m1:answer")
        assert original.payload["content"].startswith("请以后")
        correction = db.scalar(select(MemoryFact).where(MemoryFact.source_ref == result["correction_source_ref"]))
        assert correction.payload["concept_id"] == "math-a"
    with pytest.raises(ValueError):
        service.forget("m1", "concept", "physics-a")


def test_disabled_scope_and_other_student_never_leak(database):
    add_concept(database)
    add_concept(database, "other", "math-a", text="另一个学生私有观察")
    service = HierarchicalMemoryService(ScopeModel(), database)
    assert "另一个学生" not in json.dumps(service.context_for_session("m1"), ensure_ascii=False)
    service.set_enabled("m1", False, "concept", "math-a")
    assert not service.context_for_session("m1")["concept_memories"]
    service.set_enabled("m1", True, "concept", "math-a")
    assert service.context_for_session("m1")["concept_memories"]  # 暂停与遗忘不同，保留暂停前的正文。


def test_failed_review_is_retried_by_idle_worker_without_new_chat(database):
    add_preference(database)

    class FailingReview(ScopeModel):
        def __init__(self):
            super().__init__()
            self.failed = False

        async def chat(self, messages, **kwargs):
            data = json.loads(messages[1]["content"])
            if "candidates" in data and not self.failed:
                self.failed = True
                raise RuntimeError("temporary provider failure")
            return await super().chat(messages, **kwargs)

    model = FailingReview()
    service = HierarchicalMemoryService(model, database)
    with pytest.raises(RuntimeError):
        asyncio.run(service.aggregate("m1"))
    with database() as db:
        job = db.scalar(select(MemoryJob).where(MemoryJob.status == "retry"))
        assert job.result_snapshot["owner"]["student_id"] == "s"
        assert job.metrics["model_calls"] == 2
        assert job.metrics["usage_unavailable_calls"] == 1
        job.available_at = now()
    assert asyncio.run(run_pending(service)) is True
    assert service.read("m1")["course_memory"]["content"]
    with database() as db:
        job = db.scalar(select(MemoryJob).where(MemoryJob.status == "completed"))
        assert job.attempts == 2
        assert job.metrics["model_calls"] == 4
    assert asyncio.run(run_pending(service)) is False


def test_recovery_only_releases_existing_scope_jobs(database):
    add_preference(database)
    model = ScopeModel()
    service = HierarchicalMemoryService(model, database)
    with database() as db:
        snapshot = service._snapshot(db, "s", "course", "math")
        assert snapshot
    assert queue_for(service).recover()["scope_jobs_recovered"] == 1
    assert not model.calls
    assert asyncio.run(run_pending(service)) is True
    assert service.read("m1")["course_memory"]["content"]


def test_review_can_use_distinct_injected_model(database):
    add_preference(database)
    extract, reviewer = ScopeModel(), ScopeModel()
    service = HierarchicalMemoryService(extract, database, review_models=reviewer)
    asyncio.run(service.aggregate("m1"))
    assert all("candidates" not in data for data, _ in extract.calls)
    assert len(reviewer.calls) == 1 and "candidates" in reviewer.calls[0][0]


def test_forged_absent_concept_manifest_is_rejected(database):
    service = HierarchicalMemoryService(ScopeModel(), database)
    context = service.context_for_session("m1")
    manifest = context["memory_manifest"]
    concept = next(item for item in manifest if item["scope_kind"] == "concept")
    concept["scope_key"] = "concept:forged"
    assert not service.validate_context(manifest)


def test_pause_does_not_backfill_events_collected_while_disabled(database):
    add_concept(database)
    service = HierarchicalMemoryService(ScopeModel(), database)
    service.set_enabled("m1", False, "concept", "math-a")
    with database() as db:
        db.add(MemoryFact(id="paused-fact", session_id="m1", seq=2,
            source_ref="paused:answer", kind="message", payload={"role": "user", "content": "暂停期间的作答"}))
    service.set_enabled("m1", True, "concept", "math-a")
    with database() as db:
        state = db.get(ScopeMemory, scope_key("s", "concept", "math", "math-a"))
        assert allowed_sources(db, state, ["m1:answer", "paused:answer"]) == {"m1:answer"}
    assert service.context_for_session("m1")["concept_memories"]


def test_revoked_or_removed_inputs_settle_old_retry_job(database):
    add_preference(database)
    service = HierarchicalMemoryService(ScopeModel(), database)
    with database() as db:
        snapshot = service._snapshot(db, "s", "course", "math")
        job = db.get(MemoryJob, snapshot["job_id"])
        job.status, job.lease_until = "retry", None
        db.delete(db.get(SessionMemoryExtraction, "m1"))
        bump_scope(db, scope_key("s", "course", "math"), invalidate=True)
    assert asyncio.run(run_pending(service)) is True
    assert asyncio.run(run_pending(service)) is False
    with database() as db:
        assert db.get(MemoryJob, snapshot["job_id"]).status == "superseded"


def test_same_message_preferences_bind_atomic_scope_not_shared_fact_id(database):
    add_preference(database, scope="student", text="所有课程以后先简短解释")
    with database() as db:
        extraction = db.get(SessionMemoryExtraction, "m1")
        global_pref = extraction.preferences[0]
        course_pref = {**global_pref, "claim_id": "course-pref", "scope_kind": "course",
                       "text": "仅代数课程先列符号式"}
        extraction.preferences = [global_pref, course_pref]

    class WrongScopeModel(ScopeModel):
        async def chat(self, messages, **kwargs):
            data = json.loads(messages[1]["content"])
            if "candidates" in data:
                # 即便独立审查犯错全部keep，服务ID约束也不能被绕过。
                return await super().chat(messages, **kwargs)
            pref = data["preferences"][0] if data["preferences"] else None
            wrong = {"text": "仅代数课程先列符号式", "source_refs": ["pref:m1"],
                     "explicit_preference": True, "preference_id": "course-pref"}
            legitimate = {**wrong, "preference_id": pref["claim_id"]} if pref else None
            result = {"claims": [wrong, legitimate] if data["scope_kind"] == "student" else [legitimate], "no_change": False}
            self.calls.append((deepcopy(data), kwargs))
            return {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 30}}

    model = WrongScopeModel()
    service = HierarchicalMemoryService(model, database)
    asyncio.run(service.aggregate("m1"))
    global_memory = service.read("m1")["global_memory"]
    assert global_memory["content"] == "- 所有课程以后先简短解释"
    assert "代数" not in global_memory["content"]
    assert service.read("m1")["course_memory"]["content"] == "- 仅代数课程先列符号式"
    claim = global_memory["claims"][0]
    assert claim["preference_id"] == "pref-m1" and claim["preference_scope"] == "student"
    reviews = [data for data, _ in model.calls if "candidates" in data and data["scope_kind"] == "student"]
    assert reviews and list(reviews[0]["authorized_preferences"]) == ["pref-m1"]


def test_old_unbound_upper_preference_is_hidden_before_refresh(database):
    with database() as db:
        state = ensure_scope_state(db, "s", "student")
        put_scope(db, scope_key=state.scope_key, student_id="s", scope_kind="student", course_id="", concept_id="",
            content="课程特定偏好被旧版误写到总体", claims=[{"claim_id": "old", "text": "课程特定偏好被旧版误写到总体",
                "source_refs": ["old-ref"], "explicit_preference": True}], source_refs=["old-ref"], version=1)
    service = HierarchicalMemoryService(ScopeModel(), database)
    assert not service.read("m1")["global_memory"]["content"]
    assert service.context_for_session("m1")["global_memory"] is None


def test_first_concept_aggregation_failure_is_visible_without_memory_row(database):
    from app.domains.memory.scopes.models import SessionConceptObservation

    with database() as db:
        key = scope_key("s", "concept", "math", "math-a")
        db.add(SessionConceptObservation(id="obs", session_id="m1", student_id="s", course_id="math",
            concept_id="math-a", claims=[], source_refs=[], content="", version=1))
        db.add(MemoryJob(job_kind="scope_aggregate", id="failed-concept-job", scope_key=key, rule_version="business-memory-v1",
            source_generation=1, status="failed", attempts=3, error="ValueError"))
    service = HierarchicalMemoryService(ScopeModel(), database)
    snapshot = service.read("m1")
    assert any(job["id"] == "failed-concept-job" and job["status"] == "failed" for job in snapshot["scope_jobs"])
    assert snapshot["scope_job_summary"]["statuses"]["failed"] == 1


def test_rule_upgrade_settles_old_pending_jobs_without_losing_metrics(database):
    with database() as db:
        db.add(MemoryJob(job_kind="scope_aggregate", id="old-scope", scope_key=scope_key("s", "student"),
            rule_version="business-scope-v1", source_generation=0, status="retry", attempts=1,
            metrics={"model_calls": 2, "prompt_tokens": 123}))
    service = HierarchicalMemoryService(ScopeModel(), database)
    assert queue_for(service).recover()["scope_jobs_superseded"] == 1
    with database() as db:
        job = db.get(MemoryJob, "old-scope")
        assert job.status == "superseded" and job.metrics["prompt_tokens"] == 123
    assert asyncio.run(run_pending(service)) is False


def test_paused_concept_observations_are_hidden_in_read_snapshot(database):
    from app.domains.memory.scopes.models import SessionConceptObservation

    add_concept(database)
    with database() as db:
        db.add(SessionConceptObservation(id="obs-paused", session_id="m1", student_id="s", course_id="math",
            concept_id="math-a", content="暂停前的私有观察", source_refs=["m1:answer"], claims=[], version=1))
    service = HierarchicalMemoryService(ScopeModel(), database)
    assert service.read("m1")["concept_observations"]
    service.set_enabled("m1", False, "concept", "math-a")
    assert not service.read("m1")["concept_observations"]


def test_known_child_id_is_provenance_and_never_parent_identity(database):
    add_concept(database)
    add_concept(database, "m2", "math-b")

    class ChildIdModel(ScopeModel):
        async def chat(self, messages, **kwargs):
            response = await super().chat(messages, **kwargs)
            data = json.loads(messages[1]["content"])
            if "candidates" not in data:
                payload = json.loads(response["choices"][0]["message"]["content"])
                payload["claims"][0]["existing_claim_id"] = data["children"][0]["claims"][0]["claim_id"]
                response["choices"][0]["message"]["content"] = json.dumps(payload, ensure_ascii=False)
            return response

    service = HierarchicalMemoryService(ChildIdModel(), database)
    asyncio.run(service.aggregate("m1"))
    claim = service.read("m1")["course_memory"]["claims"][0]
    assert claim["source_claim_ids"] in [["claim-m1"], ["claim-m2"]]
    assert claim["claim_id"] not in {"claim-m1", "claim-m2"}


def test_known_preference_id_is_not_reused_as_parent_id(database):
    add_preference(database)

    class PreferenceIdModel(ScopeModel):
        async def chat(self, messages, **kwargs):
            response = await super().chat(messages, **kwargs)
            data = json.loads(messages[1]["content"])
            if "candidates" not in data:
                payload = json.loads(response["choices"][0]["message"]["content"])
                payload["claims"][0]["existing_claim_id"] = data["preferences"][0]["claim_id"]
                response["choices"][0]["message"]["content"] = json.dumps(payload, ensure_ascii=False)
            return response

    service = HierarchicalMemoryService(PreferenceIdModel(), database)
    asyncio.run(service.aggregate("m1"))
    claim = service.read("m1")["course_memory"]["claims"][0]
    assert claim["claim_id"] != "pref-m1" and claim["source_claim_ids"] == ["pref-m1"]


def test_unknown_id_rejects_only_that_item_and_keeps_valid_preference(database):
    add_preference(database)

    class MixedIdModel(ScopeModel):
        async def chat(self, messages, **kwargs):
            response = await super().chat(messages, **kwargs)
            data = json.loads(messages[1]["content"])
            if "candidates" not in data:
                payload = json.loads(response["choices"][0]["message"]["content"])
                forged = {**payload["claims"][0], "existing_claim_id": "never-provided-id"}
                payload["claims"].insert(0, forged)
                response["choices"][0]["message"]["content"] = json.dumps(payload, ensure_ascii=False)
            return response

    service = HierarchicalMemoryService(MixedIdModel(), database)
    asyncio.run(service.aggregate("m1"))
    assert service.read("m1")["course_memory"]["content"] == "- 本课程以后请先给提示"
    with database() as db:
        job = db.scalar(select(MemoryJob).where(MemoryJob.status == "completed"))
        assert job.result_snapshot["review"]["validator_rejections"] == [{"index": 0, "reason": "unknown_existing_claim_id"}]


def test_all_unknown_ids_retry_with_fixed_reason_and_no_memory_write(database):
    add_preference(database)

    class ForgedIdModel(ScopeModel):
        async def chat(self, messages, **kwargs):
            response = await super().chat(messages, **kwargs)
            data = json.loads(messages[1]["content"])
            if "candidates" not in data:
                payload = json.loads(response["choices"][0]["message"]["content"])
                payload["claims"][0]["existing_claim_id"] = "never-provided-id"
                response["choices"][0]["message"]["content"] = json.dumps(payload, ensure_ascii=False)
            return response

    service = HierarchicalMemoryService(ForgedIdModel(), database)
    with pytest.raises(ValueError):
        asyncio.run(service.aggregate("m1"))
    assert not service.read("m1")["course_memory"]["content"]
    with database() as db:
        job = db.scalar(select(MemoryJob).where(MemoryJob.status == "retry"))
        assert job.result_snapshot["failure_reason_code"] == "all_candidates_rejected"


def test_parent_scope_revocation_filters_model_children_without_deleting_concepts(database):
    add_concept(database)
    add_concept(database, "m2", "math-b")
    add_preference(database)
    model = ScopeModel()
    service = HierarchicalMemoryService(model, database)
    asyncio.run(service.aggregate("m1"))
    service.forget("m1", "course")
    with database() as db:
        db.add(MemoryFact(id="new-course-correction", session_id="m1", seq=100,
            source_ref="correction:new-course", kind="correction", payload={"instruction": "本课程今后先画图",
                "scope_kind": "course", "_source_created_at": now().isoformat()}))
        extraction = db.get(SessionMemoryExtraction, "m1")
        extraction.preferences = [*extraction.preferences, {"claim_id": "new-course-pref", "scope_kind": "course",
            "text": "本课程今后先画图", "source_refs": ["correction:new-course"],
            "source_quotes": [{"source_ref": "correction:new-course", "quote": "本课程今后先画图"}], "explicit": True}]
        extraction.version += 1
        bump_scope(db, scope_key("s", "course", "math"))
    before = len(model.calls)
    asyncio.run(service.aggregate("m1"))
    course_input = next(data for data, _ in model.calls[before:] if data.get("scope_kind") == "course" and "children" in data)
    assert course_input["children"] == []
    assert set(course_input["facts"]) == {"correction:new-course"}
    snapshot = service.read("m1")
    assert snapshot["course_memory"]["content"] == "- 本课程今后先画图"
    assert all(item["content"] for item in snapshot["concept_memories"])


def queue_for(hierarchy):
    # 测试通过业务门面装配统一队列，聚合层不反向依赖门面。
    from app.domains.memory.service import MemoryService
    from app.domains.memory.recording.jobs import MemoryWorker
    service = getattr(hierarchy, "queue_service", None)
    if service is None:
        service = MemoryService(hierarchy.models)
        service.database = hierarchy.database
        service.business_pipeline.hierarchy = hierarchy
        hierarchy.queue_service = service
    return service


async def run_pending(hierarchy):
    from app.domains.memory.recording.jobs import MemoryWorker
    service = queue_for(hierarchy)
    return await (service.worker or MemoryWorker(service)).run_once()
