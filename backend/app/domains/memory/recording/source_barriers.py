"""惰性继承旧撤销语义；新版作用域遗忘不借此扩大影响范围。"""

from app.domains.memory.scopes.models import ScopeMemory, SessionMemoryExtraction
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import select, update

from app.infrastructure.persistence.defaults import now
from app.domains.memory.models import MemoryFact
from app.domains.memory.scopes.identity import scope_key
from app.domains.sessions.models import LearningSession

MARKER = "legacy-barrier-v1:"


def _time(value):
    try:
        result = datetime.fromisoformat(str(value))
        return (
            result.astimezone(timezone.utc).replace(tzinfo=None)
            if result.tzinfo
            else result
        )
    except (TypeError, ValueError):
        return None


def _parts(refs):
    dates = [_time(ref[7:]) for ref in refs if ref.startswith("before:")]
    return {ref for ref in refs if not ref.startswith("before:")}, max(
        (value for value in dates if value), default=None
    )


def _descriptor(db, student_id, course_id):
    """同一短事务缓存来源集合；不会在进程中缓存撤销状态。"""
    root = db.get(ScopeMemory, scope_key(student_id, "student"))
    legacy = root.legacy_barrier if root else {}
    refs, cutoff = _parts((legacy or {}).get("blocked_source_refs", []))
    query = select(SessionMemoryExtraction).where(
        SessionMemoryExtraction.student_id == student_id
    )
    if course_id:
        query = query.where(SessionMemoryExtraction.course_id == course_id)
    for experience in db.scalars(query):
        blocked, session_cutoff = _parts(experience.blocked_source_refs or [])
        refs.update(blocked)
        if session_cutoff:
            # 会话截止时间只转换成本会话的精确来源，不能误挡其它旧会话。
            for fact in db.scalars(
                select(MemoryFact).where(MemoryFact.session_id == experience.session_id)
            ):
                source_time = (
                    _time(fact.payload.get("_source_created_at")) or fact.created_at
                )
                if source_time <= session_cutoff:
                    refs.add(fact.source_ref)
    descriptor = (refs, cutoff)
    return descriptor


def inherited_allowed_refs(db, student_id, course_id, refs):
    """旧全范围撤销不再送入任何新提取；新版单scope屏障不参与这里。"""
    blocked, cutoff = _descriptor(db, student_id, course_id)
    wanted = set(refs) - blocked
    if cutoff is None:
        return wanted
    query = (
        select(MemoryFact)
        .join(LearningSession, LearningSession.id == MemoryFact.session_id)
        .where(
            LearningSession.student_id == student_id,
            LearningSession.course_id == course_id,
            MemoryFact.source_ref.in_(wanted),
        )
    )
    return {
        fact.source_ref
        for fact in db.scalars(query)
        if (_time(fact.payload.get("_source_created_at")) or fact.created_at) > cutoff
    }
