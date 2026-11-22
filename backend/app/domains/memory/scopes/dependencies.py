"""业务作用域、来源屏障和派生失效；摘要不修改原始学习证据。"""

from app.domains.memory.scopes.models import ScopeMemory
import hashlib
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.domains.courses.models import Question
from app.domains.sessions.models import LearningSession
from app.domains.memory.models import MemoryFact
from app.domains.memory.source_identity import source_task_id


from .identity import scope_key


def ensure_scope_state(
    db, student_id: str, scope_kind: str, course_id: str = "", concept_id: str = ""
):
    key = scope_key(student_id, scope_kind, course_id, concept_id)
    row = db.get(ScopeMemory, key)
    if row is None:
        try:
            with db.begin_nested():
                row = ScopeMemory(
                    scope_key=key,
                    student_id=student_id,
                    scope_kind="student" if scope_kind == "global" else scope_kind,
                    course_id="" if scope_kind in {"global", "student"} else course_id,
                    concept_id=concept_id if scope_kind == "concept" else "",
                    generation=0,
                    invalidation_version=0,
                    enabled=True,
                    blocked_source_refs=[],
                    blocked_claim_ids=[],
                )
                db.add(row)
                db.flush()
        except IntegrityError:
            # 并发会话可能先建同一scope；回读当前已提交行，不回滚外层学习事实。
            row = db.scalar(
                select(ScopeMemory)
                .where(ScopeMemory.scope_key == key)
                .with_for_update()
            )
            if row is None:
                raise
    return row


def allowed_sources(db, state, refs) -> set[str]:
    """撤销按作用域生效；同一来源仍可支持未撤销的其它知识点。"""
    from app.domains.memory.recording.source_barriers import inherited_allowed_refs

    wanted = inherited_allowed_refs(db, state.student_id, state.course_id, refs) - set(
        state.blocked_source_refs or []
    )
    if not wanted or not state.enabled:
        return set()
    if state.cutoff_at is None:
        return wanted
    facts = db.scalars(select(MemoryFact).where(MemoryFact.source_ref.in_(wanted))).all()
    # 同一引用可能出现在迁移会话；按原始发生时间而非迁移时间检查。
    result = set()
    for fact in facts:
        stamp = fact.payload.get("_source_created_at")
        source_time = str(stamp) if stamp else fact.created_at.isoformat()
        if source_time > state.cutoff_at.isoformat():
            result.add(fact.source_ref)
    return result


def source_facts(db, student_id: str, refs, course_id: str | None = None) -> dict[str, dict]:
    """所有来源必须属于服务端学生，课程记忆还要属于当前课程。"""
    if not refs:
        return {}
    query = select(MemoryFact, LearningSession).join(
        LearningSession, LearningSession.id == MemoryFact.session_id).where(
        LearningSession.student_id == student_id, MemoryFact.source_ref.in_(set(refs)))
    if course_id is not None:
        query = query.where(LearningSession.course_id == course_id)
    result = {}
    for fact, session in db.execute(query):
        payload = fact.payload or {}
        question = db.get(Question, payload.get("question_id")) if payload.get("question_id") else None
        task = source_task_id(payload, session.course_id, catalog_question=question, source_kind=fact.kind)
        result[fact.source_ref] = {"source_ref": fact.source_ref, "session_id": session.id,
            "course_id": session.course_id, "task_id": task, "kind": fact.kind,
            "seq": fact.seq, "payload": payload, "created_at": fact.created_at.isoformat()}
    return result
