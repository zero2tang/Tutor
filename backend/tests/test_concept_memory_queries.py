from app.domains.memory.models import MemoryJob
from app.domains.memory.scopes.models import SessionMemoryExtraction
from reference_helpers import put_scope
"""知识点下钻必须同时守住学生、课程、当前目标和撤销范围。"""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.infrastructure.persistence.database import Base
from app.domains.memory.scopes.dependencies import ensure_scope_state, scope_key
from app.domains.sessions.models import LearningSession
from app.domains.memory.scopes.models import ScopeMemory, SessionConceptObservation
from app.domains.memory.concepts.queries import concept_memory_query
from app.domains.memory.visibility import public_memory_snapshot
from app.agent.tools.contracts import ToolContext
from app.application.agent.tools import TeachingTools
from native_tools import invoke_tool


@pytest.fixture
def scoped_db(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///" + (tmp_path / "queries.db").as_posix())
    Base.metadata.create_all(engine)

    @contextmanager
    def database():
        with Session(engine, expire_on_commit=False) as db:
            yield db
            db.commit()

    monkeypatch.setattr("app.domains.memory.concepts.queries.db_session", database)
    with database() as db:
        db.add(LearningSession(id="s", student_id="alice", course_id="math", title="测试", goal="测试", concept_ids=["c"]))
        for identifier, student, course, status in (("ok", "alice", "math", "active"),
                ("other-student", "bob", "math", "active"), ("other-course", "alice", "physics", "active"),
                ("revoked", "alice", "math", "stale")):
            db.add(SessionConceptObservation(id=identifier, session_id=identifier, student_id=student,
                course_id=course, concept_id="c", content=identifier, source_refs=["fact:" + identifier],
                claims=[], status=status))
    return database


def test_observation_drilldown_is_scoped_and_honors_pause(scoped_db):
    context = ToolContext(course_id="math", session_id="s", student_id="alice", concept_ids=["c"])
    result = concept_memory_query(None, context, "c", observations=True)
    assert [row["observation_id"] for row in result["observations"]] == ["ok"]
    with scoped_db() as db:
        ensure_scope_state(db, "alice", "concept", "math", "c").enabled = False
    assert concept_memory_query(None, context, "c", observations=True)["observations"] == []


def test_read_rejects_forged_owner_and_unfrozen_target(scoped_db):
    context = ToolContext(course_id="math", session_id="s", student_id="bob", concept_ids=["c"])
    with pytest.raises(PermissionError):
        concept_memory_query(None, context, "c", observations=True)
    context = ToolContext(course_id="math", session_id="s", student_id="alice", concept_ids=["a", "b", "c", "d"])
    with pytest.raises(PermissionError):
        concept_memory_query(None, context, "d", observations=True)
    assert "get_concept_observations" not in {tool.name for tool in TeachingTools(None).for_role("assessment")}


@pytest.mark.parametrize("pending", [
    {"exercise_batch": {"items": [{"assisted": False}]}},
    {"guided_teaching": {"transfer": {"assisted": False}, "closed": False}},
])
def test_independent_public_snapshot_hides_memory_without_mutating_it(pending):
    value = {"content": "容易构成提示的记忆", "claims": [{"text": "旧观察"}], "source_refs": ["raw"]}
    snapshot = {"concept_memories": [value], "global_memory": value, "unassigned": [value]}
    result = public_memory_snapshot(snapshot, SimpleNamespace(pending_context=pending, pending_question_id=None))
    assert result["presentation_restricted"] and result["global_memory"]["content"] == ""
    assert result["concept_memories"][0]["source_refs"] == [] and "unassigned" not in result
    assert snapshot["global_memory"]["content"] and snapshot["unassigned"]


@pytest.mark.parametrize("operation", ["refresh", "close", "correct", "forget"])
@pytest.mark.asyncio
async def test_every_public_mutation_returns_the_same_redacted_view(scoped_db, monkeypatch, operation):
    from app.api.routes import memory as endpoint

    monkeypatch.setattr(endpoint, "db_session", scoped_db)
    with scoped_db() as db:
        db.get(LearningSession, "s").pending_question_id = "awaiting-answer"
    snapshot = {"global_memory": {"content": "不应从POST返回的提示", "source_refs": ["raw"], "claims": []},
                "concept_memories": [], "jobs": []}

    class Memory:
        def read(self, identifier):
            return deepcopy(snapshot)

        async def refresh(self, *args, **kwargs):
            return {"job_id": "job", **deepcopy(snapshot)}

        async def correct(self, *args, **kwargs):
            return deepcopy(snapshot)

        def forget(self, *args, **kwargs):
            return deepcopy(snapshot)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services={"memory": Memory()})))
    if operation == "refresh":
        result = await endpoint.refresh_memory("s", endpoint.RefreshRequest(), request, wait=True)
    elif operation == "close":
        result = await endpoint.close_memory("s", request, wait=True)
    elif operation == "correct":
        result = await endpoint.correct_memory("s", endpoint.CorrectionRequest(text="纠正", kind="experience"), request, wait=True)
    else:
        result = endpoint.forget_memory("s", "experience", request)
    assert result["presentation_restricted"] and result["global_memory"]["content"] == ""
