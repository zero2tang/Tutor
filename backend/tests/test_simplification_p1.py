"""第二批合表的跨目的缓存、租约隔离和私有诊断边界。"""

from datetime import timedelta
import json
import pytest
from sqlalchemy import select
from app.infrastructure.persistence.defaults import now
from app.infrastructure.persistence.model_calls import ModelCall
from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.courses.models import IndexTask
from app.domains.courses.questions.search.store import QuestionSearchStore
from app.domains.sessions.service import SessionService
from test_textbook_extraction import store, registered
from test_exercise_context import exercise_database


def test_same_hash_is_scoped_by_purpose_and_failure_never_hits_cache(store):
    _, _, job = registered(store)
    search = QuestionSearchStore(store.session)
    search.call("same", "extract", {"value":"query"}, {}, "completed")
    assert store.cached_call(job, "same") is None
    call = store.call_started(job, "page", 9, "model", "same")
    store.call_finished(call, response={"validated":{"value":"book"}})
    assert store.cached_call(job, "same")["validated"]["value"] == "book"
    assert search.cached_call("same").response == {"value":"query"}
    failed = store.call_started(job, "page", 9, "model", "failure")
    store.call_finished(failed, status="failed", error="invalid")
    assert store.cached_call(job, "failure") is None
    with store.session() as db:
        assert len(list(db.scalars(select(ModelCall)))) == 3


def test_build_lease_is_scoped_and_fenced_before_publication(store):
    search = QuestionSearchStore(store.session)
    identifier, token = search.claim_build("course", "first")
    with pytest.raises(ValueError, match="有效构建租约"):
        search.claim_build("course", "first")
    other, _ = search.claim_build("course", "second")
    assert other != identifier
    with store.session() as db:
        db.get(IndexTask, identifier).lease_until = now() - timedelta(seconds=1)
    replacement, next_token = search.claim_build("course", "first")
    assert replacement == identifier and next_token != token
    with pytest.raises(ValueError, match="过期|接管"):
        search.renew_build(identifier, token)
    with pytest.raises(ValueError, match="失效"):
        search.publish("course", "generation", "first", lease=(identifier, token))
    search.publish("course", "generation", "first", lease=(identifier, next_token))


def test_receipt_logging_failure_does_not_fail_retrieval(store, monkeypatch):
    from app.infrastructure.persistence import retrieval_log
    monkeypatch.setattr(retrieval_log.logger, "info", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    identifier = QuestionSearchStore(store.session).receipt("course", None, {"returned_ids":["q"]})
    assert len(identifier) == 32


def test_runs_exclude_private_answers_even_from_legacy_public_columns(exercise_database):
    with exercise_database() as db:
        db.add(AgentRun(id="private-run", session_id="mine", turn_id="turn", agent="exercise",
            skill="understanding", status="failed", model="test", output_summary="SECRET_ANSWER",
            error="SECRET_ANSWER", tool_calls=[{"name":"tool", "arguments":{"answer":"SECRET_ANSWER"}, "status":"failed"}],
            private_diagnostics={"generation_attempt":{"answer":"SECRET_ANSWER"}}))
    public = SessionService().runs("mine")
    assert public and "SECRET_ANSWER" not in json.dumps(public)
    assert "private_diagnostics" not in json.dumps(public)
