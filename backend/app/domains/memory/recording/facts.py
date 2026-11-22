"""为已提交业务事实分配单调序号，不从 UUID 推断业务顺序。"""

from app.domains.memory.scopes.models import (
    SessionConceptObservation,
    SessionMemoryExtraction,
)
from sqlalchemy import select

from app.domains.sessions.models import LearningSession, Message
from app.infrastructure.persistence.defaults import now
from app.domains.memory.models import MemoryFact


def state_for(db, session_id: str) -> SessionMemoryExtraction:
    # 会话行锁串行化同一会话内的水位分配；SQLite 测试依赖其写事务串行性。
    session = db.get(LearningSession, session_id, with_for_update=True)
    if session is None:
        raise ValueError("会话不存在")
    state = db.get(SessionMemoryExtraction, session_id, with_for_update=True)
    if state is None:
        state = SessionMemoryExtraction(
            session_id=session_id,
            student_id=session.student_id,
            course_id=session.course_id,
            next_seq=0,
            last_enqueued_seq=0,
            closed=False,
            close_version=0,
            invalidation_version=0,
        )
        db.add(state)
        db.flush()
    return state


def record_fact(
    db,
    session_id: str,
    source_ref: str,
    kind: str,
    payload: dict,
    *,
    reopen: bool = True,
) -> MemoryFact:
    state = state_for(db, session_id)
    old = db.scalar(select(MemoryFact).where(MemoryFact.session_id == session_id, MemoryFact.source_ref == source_ref))
    if old is not None:
        return old
    if reopen and state.closed:
        state.closed = False
        state.close_version += 1
        experience = db.get(SessionMemoryExtraction, session_id)
        if experience:
            experience.experience_status = "provisional"
            experience.experience_version += 1
    state.next_seq += 1
    row = MemoryFact(session_id=session_id, seq=state.next_seq, source_ref=source_ref, kind=kind,
                     payload={"_source_created_at": now().isoformat(), **payload})
    db.add(row)
    db.flush()
    return row


def record_message_fact(db, message: Message) -> MemoryFact:
    """与原消息同事务保存；完整正文和提交前帮助快照保持原样。"""
    metadata = {key: value for key, value in (message.metadata_json or {}).items()
                if key not in {"_guidance_receipt", "_guidance_analysis", "_previous_guidance_workspace", "_guidance_publication_audit", "_input_hash"}}
    guidance = (message.metadata_json or {}).get("_guidance_receipt", {}).get("guidance")
    if guidance:
        # 回执中的完整资料和旧工作区不重复输入，但题目和帮助条件要准确保留。
        metadata["guidance_snapshot"] = {key: guidance.get(key) for key in (
            "id", "version", "problem", "transfer", "pending_question", "pending_question_id",
            "response_expectation", "help_level", "assisted", "closed")}
    return record_fact(db, message.session_id, "message:" + message.id, "message", {
        "role": message.role, "agent": message.agent, "content": message.content,
        "metadata": metadata, "citations": message.citations or [],
        "_source_created_at": message.created_at.isoformat(),
    })


def record_evidence_fact(db, link) -> MemoryFact:
    """评估归因与事实同事务提交；修订保留历史并立即撤销依赖旧版本的记忆。"""
    ref = f"evidence_link:{link.id}:v{link.version}"
    existing = db.scalar(select(MemoryFact).where(MemoryFact.session_id == link.session_id,
                                                 MemoryFact.source_ref == ref))
    if existing is not None:
        return existing
    old_refs = list(db.scalars(select(MemoryFact.source_ref).where(MemoryFact.session_id == link.session_id,
        MemoryFact.source_ref.like(f"evidence_link:{link.id}:v%"))))
    if old_refs:
        from app.domains.memory.scopes.invalidation import bump_scope, invalidate_dependents
        from app.domains.memory.scopes.dependencies import ensure_scope_state

        state = ensure_scope_state(db, link.student_id, "concept", link.course_id, link.concept_id)
        state.blocked_source_refs = list(dict.fromkeys([*(state.blocked_source_refs or []), *old_refs]))
        bump_scope(db, state.scope_key, invalidate=True)
        observation = db.scalar(select(SessionConceptObservation).where(
            SessionConceptObservation.session_id == link.session_id,
            SessionConceptObservation.concept_id == link.concept_id))
        if observation and set(old_refs).intersection(observation.source_refs or []):
            observation.status = "stale"
            observation.version += 1
            invalidate_dependents(db, "observation", observation.id, old_refs)
    return record_fact(db, link.session_id, ref, "evidence_link", {
        "source_ref": link.source_ref, "source_version": link.source_version,
        "evidence_group_id": link.evidence_group_id, "canonical_item_id": link.canonical_item_id,
        "concept_id": link.concept_id, "dimension": link.dimension, "criterion_id": link.criterion_id,
        "source_kind": link.source_kind, "attribution": link.attribution,
        "observed_result": link.observed_result, "source_span_refs": link.source_span_refs,
        "help_snapshot": link.help_snapshot, "eligible_for_mastery": link.eligible_for_mastery,
        "assessment": {key: value for key, value in (link.payload or {}).items()
                       if key not in {"legacy_baseline", "superseded_by_packet"}},
        "_source_created_at": link.created_at.isoformat(),
    })
