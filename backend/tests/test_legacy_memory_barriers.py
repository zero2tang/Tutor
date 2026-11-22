from app.domains.memory.models import MemoryJob
from app.domains.memory.scopes.models import SessionMemoryExtraction
from reference_helpers import put_scope
"""旧版撤销的惰性兼容，不把新版学生scope遗忘广播到其它作用域。"""

import asyncio
from copy import deepcopy
import json

from sqlalchemy import select

from test_concept_memory import Model, add_fact, claim, database, response_for
from app.domains.memory.concepts.service import BusinessMemoryPipeline
from app.domains.memory.scopes.dependencies import allowed_sources, ensure_scope_state, scope_key
from app.domains.memory.concepts.observations import RULE_VERSION
from app.domains.memory.service import MemoryService
from app.domains.memory.context import read_context, validate_manifest
from app.domains.memory.scopes.models import ScopeMemory, SessionConceptObservation
from app.domains.memory.models import MemoryFact, MemoryJob


def test_old_student_forget_blocks_all_new_scopes_once_and_new_facts_remain_allowed(database):
    add_fact(database, "s1", "旧事实分配律漏乘")
    model = Model(response_for())
    service = MemoryService(model)
    legacy_forget(service,"s1")
    BusinessMemoryPipeline(model, service)
    result = asyncio.run(service.refresh("s1"))
    assert not model.calls
    assert not result["session_concept_observations"]
    with database() as db:
        for kind, course, concept in [("student", "", ""), ("course", "math", ""), ("concept", "math", "c1")]:
            state = ensure_scope_state(db, "alice", kind, course, concept)
            versions = (state.generation, state.invalidation_version)
            assert not allowed_sources(db, state, ["message:s1-answer"])
            ensure_scope_state(db, "alice", kind, course, concept)
            assert (state.generation, state.invalidation_version) == versions
    add_fact(database, "s1", "新事实分配律完成", "new-answer")
    result = asyncio.run(service.refresh("s1"))
    assert result["session_concept_observations"][0]["source_refs"] == ["message:new-answer"]


def test_old_experience_cutoff_does_not_block_other_old_session(database):
    add_fact(database, "s1", "本会话旧观察")
    add_fact(database, "s2", "另一个会话旧观察")
    service = MemoryService(Model())
    service.forget("s1", "experience")
    with database() as db:
        state = ensure_scope_state(db, "alice", "concept", "math", "c1")
        assert allowed_sources(db, state, ["message:s1-answer", "message:s2-answer"]) == {"message:s2-answer"}
        assert state.cutoff_at is None


def test_legacy_migration_hides_existing_v2_body_and_invalidates_manifest(database):
    add_fact(database, "s1", "分配律旧观察")
    with database() as db:
        state = ensure_scope_state(db, "alice", "concept", "math", "c1")
        put_scope(db, scope_key=state.scope_key, student_id="alice", scope_kind="concept", course_id="math",
            concept_id="c1", content="旧观察", claims=[], source_refs=["message:s1-answer"], version=1)
    with database() as db:
        old = read_context(db, "alice", "math", ["c1"])
        assert old["concept_memories"]
    legacy_forget(MemoryService(Model()),"s1")
    with database() as db:
        current = read_context(db, "alice", "math", ["c1"])
        assert current["concept_memories"] == []
        assert db.get(ScopeMemory, scope_key("alice", "concept", "math", "c1")).status == "stale"
        assert not validate_manifest(db, old["memory_manifest"])


def test_new_student_scope_forget_does_not_create_legacy_all_scope_barrier(database):
    add_fact(database, "s1", "当前学习事实")
    with database() as db:
        student = ensure_scope_state(db, "alice", "student")
        student.blocked_source_refs = ["message:s1-answer"]
        concept = ensure_scope_state(db, "alice", "concept", "math", "c1")
        assert allowed_sources(db, concept, ["message:s1-answer"]) == {"message:s1-answer"}


class CopyChildModel(Model):
    async def chat(self, messages, **kwargs):
        response = await super().chat(messages, **kwargs)
        data = json.loads(messages[1]["content"])
        if "old_claims" in data:
            result = json.loads(response["choices"][0]["message"]["content"])
            result["claims"][0]["claim_id"] = data["observations"][0]["claims"][0]["claim_id"]
            response["choices"][0]["message"]["content"] = json.dumps(result, ensure_ascii=False)
        return response


def test_known_child_claim_id_is_converted_to_new_parent_id(database):
    add_fact(database, "s1", "分配律漏乘")
    model = CopyChildModel(response_for())
    service = MemoryService(model)
    BusinessMemoryPipeline(model, service)
    asyncio.run(service.refresh("s1"))
    with database() as db:
        observation = db.scalar(select(SessionConceptObservation))
        memory = db.get(ScopeMemory, scope_key("alice", "concept", "math", "c1"))
        assert memory and memory.claims[0]["claim_id"] != observation.claims[0]["claim_id"]
        assert len(model.calls) == 2


def test_rule_upgrade_explicit_refresh_rebuilds_old_failed_concept_without_new_fact(database):
    add_fact(database, "s1", "分配律漏乘")
    model = CopyChildModel(response_for())
    service = MemoryService(model)
    pipeline = BusinessMemoryPipeline(model, service)
    asyncio.run(service.refresh("s1"))
    with database() as db:
        memory = db.get(ScopeMemory, scope_key("alice", "concept", "math", "c1"))
        db.delete(memory)
        job = db.scalar(select(MemoryJob))
        job.rule_version, job.status, job.attempts = "business-memory-v1", "failed", 5
        leaf = db.scalar(select(MemoryJob))
        leaf.extractor_version = "business-memory-v1"
    calls = len(model.calls)
    asyncio.run(service.refresh("s1"))
    with database() as db:
        assert db.get(ScopeMemory, scope_key("alice", "concept", "math", "c1"))
        assert db.scalar(select(MemoryJob).where(MemoryJob.rule_version == RULE_VERSION)).status == "completed"
    assert len(model.calls) == calls + 1


def test_disabled_concept_is_hidden_from_session_observation_projection(database):
    add_fact(database, "s1", "分配律漏乘")
    model = Model(response_for())
    service = MemoryService(model)
    pipeline = BusinessMemoryPipeline(model, service)
    asyncio.run(service.refresh("s1"))
    with database() as db:
        state = ensure_scope_state(db, "alice", "concept", "math", "c1")
        state.enabled = False
    assert pipeline.read("s1")["session_concept_observations"] == []


def legacy_forget(service, session_id):
    # 仅模拟迁移前的宽屏障；新学生作用域遗忘不能再广播给下层。
    service.capture(session_id)
    from app.domains.sessions.models import LearningSession
    from app.infrastructure.persistence.defaults import now
    with service.database() as db:
        student = db.get(LearningSession,session_id).student_id
        refs = list(db.scalars(select(MemoryFact.source_ref).join(LearningSession,
            LearningSession.id==MemoryFact.session_id).where(LearningSession.student_id==student)))
        root = ensure_scope_state(db,student,"student")
        root.legacy_barrier = {"blocked_source_refs":[*refs,"before:"+now().isoformat()]}
        for row in db.scalars(select(ScopeMemory).where(ScopeMemory.student_id==student)):
            row.status = "stale"
            row.invalidation_version += 1
