from app.domains.memory.models import MemoryJob
from app.domains.memory.scopes.models import SessionMemoryExtraction
from reference_helpers import put_scope
"""验证业务记忆的来源约束、实际模型接口、跨会话归并与撤销隔离。"""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.infrastructure.persistence.database import Base
from app.domains.memory.concepts.service import BusinessMemoryPipeline
from app.domains.memory.scopes.invalidation import bump_scope
from app.domains.memory.scopes.dependencies import ensure_scope_state, scope_key
from app.domains.memory.recording.facts import record_message_fact
from app.domains.memory.concepts.observations import Extraction, ObservationExtractor, RULE_VERSION
from app.domains.memory.service import MemoryService
from app.domains.memory.scopes.models import ScopeMemory, SessionConceptObservation, SessionMemoryExtraction
from app.domains.courses.models import Concept
from app.domains.sessions.models import LearningSession, Message
from app.domains.students.models import StudentState
from app.domains.memory.models import MemoryJob


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + (tmp_path / "concept.db").as_posix())
    Base.metadata.create_all(engine)

    @contextmanager
    def session():
        with Session(engine, expire_on_commit=False) as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
    monkeypatch.setattr("app.domains.memory.service.db_session", session)
    with session() as db:
        db.add_all([Concept(id="c1", course_id="math", source_id="c1", name="分配律"),
                    Concept(id="c2", course_id="math", source_id="c2", name="等式性质"),
                    Concept(id="physics:c1", course_id="physics", source_id="p1", name="力")])
        for sid in ("s1", "s2"):
            db.add(LearningSession(id=sid, student_id="alice", course_id="math", title="测试", goal="解方程",
                concept_ids=["c1", "c2"]))
    yield session
    engine.dispose()


def add_fact(database, sid, text, mid=None):
    with database() as db:
        message = Message(id=mid or sid + "-answer", session_id=sid, role="user", content=text)
        db.add(message)
        db.flush()
        record_message_fact(db, message)


def claim(ref, quote, text="漏乘常数项，提示后修正", **changes):
    return {"claim_id": None, "text": text, "source_refs": [ref],
            "span_refs": [{"source_ref": ref, "quote": quote}], "help_condition": "assisted",
            "source_kind": "model_observation", **changes}


class Model:
    def __init__(self, callback=None, aggregate_hook=None):
        self.calls, self.callback, self.aggregate_hook = [], callback, aggregate_hook

    async def chat(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), kwargs))
        data = json.loads(messages[1]["content"])
        if "observations" in data and "old_claims" in data:
            if self.aggregate_hook:
                hook, self.aggregate_hook = self.aggregate_hook, None
                hook()
            claims = []
            for observation in data["observations"]:
                claims.extend({**item, "claim_id": None} for item in observation["claims"])
            result = {"claims": claims[-6:]}
        else:
            result = self.callback(data) if self.callback else {"observations": [], "preferences": [],
                "experience": "本会话仅提出学习要求，尚无知识点实际作答。",
                "experience_source_refs": [data["facts"][0]["source_ref"]]}
        return {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50}}


def response_for(concept="c1", text="漏乘常数项，提示后修正"):
    def callback(data):
        ref = data["facts"][0]["source_ref"]
        fragment = data["facts"][0]["fragment"]
        quote = json.loads(fragment)["content"]
        return {"observations": [{"concept_id": concept, "claims": [claim(ref, quote, text)]}],
                "experience": text, "experience_source_refs": [ref]}
    return callback


def test_planned_concepts_are_not_learning_observations(database):
    add_fact(database, "s1", "今天准备学习这两个知识点")
    model = Model()
    service = MemoryService(model)
    BusinessMemoryPipeline(model, service)
    result = asyncio.run(service.refresh("s1"))
    assert result["session_concept_observations"] == []
    assert len(model.calls) == 1
    assert model.calls[0][1]["json_output"] is True
    with database() as db:
        assert db.scalar(select(func.count()).select_from(StudentState)) == 0
        assert db.get(ScopeMemory, scope_key("alice", "student")).content == ""
        assert db.scalar(select(MemoryJob.extractor_version)) == RULE_VERSION


def test_one_session_multiple_concepts_do_not_broadcast(database):
    add_fact(database, "s1", "分配律这里漏乘了2，提示后改成2x+2；等式两边同时减2。")
    model = Model(response_for())
    service = MemoryService(model)
    BusinessMemoryPipeline(model, service)
    first = asyncio.run(service.refresh("s1"))
    assert len(first["session_concept_observations"]) == 1
    row = first["session_concept_observations"][0]
    assert row["concept_id"] == "c1"
    assert row["claims"][0]["claim_id"]
    assert row["claims"][0]["help_condition"] == "assisted"
    calls = len(model.calls)
    asyncio.run(service.refresh("s1"))
    assert len(model.calls) == calls
    with database() as db:
        memory = db.get(ScopeMemory, scope_key("alice", "concept", "math", "c1"))
        assert memory and memory.source_refs == ["message:s1-answer"]
        assert not db.get(ScopeMemory, scope_key("alice", "concept", "math", "c2")).content
        assert db.scalar(select(func.count()).select_from(StudentState)) == 0


def test_two_sessions_preserve_conflicting_observations(database):
    model = Model(response_for(text="提示后能修正分配律"))
    service = MemoryService(model)
    BusinessMemoryPipeline(model, service)
    add_fact(database, "s1", "提示后我改成2x+2")
    asyncio.run(service.refresh("s1"))
    model.callback = response_for(text="新题仍独立漏乘常数项，需继续验证")
    add_fact(database, "s2", "新题我写成3x+1")
    asyncio.run(service.refresh("s2"))
    with database() as db:
        memory = db.get(ScopeMemory, scope_key("alice", "concept", "math", "c1"))
        assert set(memory.source_refs) == {"message:s1-answer", "message:s2-answer"}
        assert len(memory.claims) == 2
        assert "需继续验证" in memory.content
        assert db.scalar(select(func.count()).select_from(SessionConceptObservation)) == 2
        assert db.scalar(select(func.count()).select_from(StudentState)) == 0


@pytest.mark.parametrize("change", [
    {"source_refs": ["message:foreign"]},
    {"span_refs": [{"source_ref": "message:a", "quote": "不存在的原文"}]},
    {"claim_id": "model-invented"},
    {"source_kind": "graded_attempt"},
])
def test_candidate_provenance_and_grading_boundary(change):
    fact = {"seq": 1, "source_ref": "message:a", "kind": "message", "payload": {"role": "user", "content": "原答2x+1"}}
    result = Extraction.model_validate({"observations": [{"concept_id": "c1",
        "claims": [claim("message:a", "原答2x+1", **change)]}]})
    with pytest.raises(ValueError):
        ObservationExtractor.validate(result, {"message:a": fact}, {}, [], {"c1"}, "math")


def test_course_foreign_concept_and_assistant_preference_rejected():
    facts = {"message:a": {"source_ref": "message:a", "kind": "message", "payload": {"role": "assistant", "content": "你总喜欢提示"}}}
    foreign = Extraction.model_validate({"observations": [{"concept_id": "physics:c1", "claims": [claim("message:a", "你总喜欢提示")]}]})
    with pytest.raises(ValueError):
        ObservationExtractor.validate(foreign, facts, {}, [], {"c1"}, "math")
    invented = Extraction.model_validate({"preferences": [{"scope_kind": "student", "text": "长期先提示",
        "source_refs": ["message:a"], "source_quotes": [{"source_ref": "message:a", "quote": "你总喜欢提示"}]}]})
    with pytest.raises(ValueError):
        ObservationExtractor.validate(invented, facts, {}, [], {"c1"}, "math")


def test_scope_generation_blocks_new_input_phantom(database):
    add_fact(database, "s1", "分配律漏乘了2")
    def concurrent_source_change():
        with database() as db:
            state = ensure_scope_state(db, "alice", "concept", "math", "c1")
            bump_scope(db, state.scope_key)
    model = Model(response_for(), aggregate_hook=concurrent_source_change)
    service = MemoryService(model)
    BusinessMemoryPipeline(model, service)
    asyncio.run(service.refresh("s1"))
    with database() as db:
        assert not db.get(ScopeMemory, scope_key("alice", "concept", "math", "c1")).content
        assert set(db.scalars(select(MemoryJob.status).where(MemoryJob.job_kind == "scope_aggregate"))) == {"superseded", "pending"}
    assert asyncio.run(service.business_pipeline.run_pending())
    with database() as db:
        assert db.get(ScopeMemory, scope_key("alice", "concept", "math", "c1")) is not None


def test_forget_one_concept_keeps_shared_source_for_other(database):
    add_fact(database, "s1", "我写成2x+1，同时两边减1")
    def two_concepts(data):
        ref = data["facts"][0]["source_ref"]
        return {"observations": [{"concept_id": cid, "claims": [claim(ref, "我写成2x+1，同时两边减1", cid)]}
                                  for cid in ["c1", "c2"]]}
    model = Model(two_concepts)
    service = MemoryService(model)
    BusinessMemoryPipeline(model, service)
    with database() as db:
        state = ensure_scope_state(db, "alice", "concept", "math", "c1")
        state.blocked_source_refs = ["message:s1-answer"]
    result = asyncio.run(service.refresh("s1"))
    assert [row["concept_id"] for row in result["session_concept_observations"]] == ["c2"]


def test_grading_claim_requires_matching_local_receipt():
    fact = {"seq": 1, "source_ref": "evidence_link:l1:v1", "kind": "evidence_link",
            "payload": {"concept_id": "c1", "attribution": "direct", "observed_result": "incorrect",
                        "assessment": {"rationale": "本步漏乘常数项"}, "help_snapshot": {"assisted": True}}}
    observation = {"concept_id": "c1", "claims": [claim(fact["source_ref"], "本步漏乘常数项", source_kind="graded_attempt")]}
    result = Extraction.model_validate({"observations": [observation]})
    accepted, _ = ObservationExtractor.validate(result, {fact["source_ref"]: fact}, {}, [], {"c1", "c2"}, "math")
    assert accepted["c1"]["claims"][0]["source_kind"] == "graded_attempt"
    observation["concept_id"] = "c2"
    with pytest.raises(ValueError):
        ObservationExtractor.validate(Extraction.model_validate({"observations": [observation]}),
            {fact["source_ref"]: fact}, {}, [], {"c1", "c2"}, "math")


def test_unassisted_claim_requires_submission_snapshot():
    fact = {"source_ref": "message:a", "kind": "message", "payload": {"role": "user", "content": "我会了"}}
    result = Extraction.model_validate({"observations": [{"concept_id": "c1",
        "claims": [claim("message:a", "我会了", help_condition="recorded_unassisted")]}]})
    with pytest.raises(ValueError):
        ObservationExtractor.validate(result, {"message:a": fact}, {}, [], {"c1"}, "math")


def test_deployment_recovery_does_not_enqueue_old_sessions(database):
    # 旧表里已有事实和水位，不代表授权启动一次全库模型回填。
    add_fact(database, "s1", "很久以前的学习内容")
    model = Model()
    service = MemoryService(model)
    BusinessMemoryPipeline(model, service)
    assert service.recover()["sessions"] == 0
    with database() as db:
        assert db.scalar(select(func.count()).select_from(MemoryJob)) == 0
    assert not model.calls
