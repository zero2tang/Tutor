"""作用域代次与派生记忆失效原语，不负责旧数据屏障迁移。"""

from app.domains.memory.scopes.models import (
    ScopeMemory,
    SessionConceptObservation,
    SessionMemoryExtraction,
)
from sqlalchemy import select, update

from app.infrastructure.persistence.defaults import now


def bump_scope(db, key: str, *, invalidate: bool = False):
    """原子增加输入集合代次，避免读改写丢失并发新增。"""
    values = {"generation": ScopeMemory.generation + 1, "updated_at": now()}
    if invalidate:
        values["invalidation_version"] = ScopeMemory.invalidation_version + 1
    db.execute(update(ScopeMemory).where(ScopeMemory.scope_key == key).values(**values))
    db.flush()
    state = db.get(ScopeMemory, key)
    db.refresh(state)
    return state


def invalidate_dependents(db, child_kind, child_key, source_refs=None):
    """固定三级祖先失效；其它课程及下层事实保持不变。"""
    if child_kind == "observation":
        child = db.get(SessionConceptObservation, child_key)
        kind = "concept"
    elif child_kind == "extraction":
        child = db.get(SessionMemoryExtraction, child_key)
        kind = "course"
    else:
        child = db.get(ScopeMemory, child_key)
        kind = child.scope_kind if child else None
    if child is None:
        return set()
    from app.domains.memory.scopes.dependencies import scope_key

    keys = []
    if child_kind == "observation":
        keys.append(
            scope_key(child.student_id, "concept", child.course_id, child.concept_id)
        )
    if kind == "concept":
        keys.append(scope_key(child.student_id, "course", child.course_id))
    if kind in {"concept", "course"}:
        keys.append(scope_key(child.student_id, "student"))
    affected = set()
    for key in keys:
        parent = db.get(ScopeMemory, key)
        if parent:
            parent.status = "stale"
            bump_scope(db, key, invalidate=True)
            affected.add(key)
    return affected
