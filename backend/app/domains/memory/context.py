"""按当前课程和少量知识点装配短记忆，撤销检查与普通正文版本分开。"""

from app.domains.memory.scopes.models import ScopeMemory
import hashlib
import json

from sqlalchemy import select

from app.domains.memory.scopes.dependencies import ensure_scope_state, scope_key
from app.domains.memory.scopes.preferences import projected_fields
from app.domains.memory.schemas import estimate_tokens
from app.domains.courses.models import Concept


def memory_value(row):
    if row is None:
        return None
    content, refs, claims = projected_fields(row)
    return {"scope_key": row.scope_key, "scope_kind": row.scope_kind, "course_id": row.course_id,
        "concept_id": row.concept_id, "content": content, "version": row.version,
        "source_generation": row.source_generation, "status": row.status,
        "source_refs": refs, "claims": claims,
        "updated_at": row.updated_at.isoformat()}


def read_context(db, student_id: str, course_id: str, concept_ids=None, *, limit=3,
                 input_budget=20000, purpose="teaching") -> dict:
    budget = max(0, min(4000, int(max(0, input_budget) * .2)))
    ids = list(dict.fromkeys(concept_ids or []))[:max(0, min(3, limit))]
    allowed = set(db.scalars(select(Concept.id).where(Concept.course_id == course_id,
                                                     Concept.id.in_(ids)))) if ids else set()
    ids = [cid for cid in ids if cid in allowed]
    keys = [("student", scope_key(student_id, "student")),
            ("course", scope_key(student_id, "course", course_id))]
    keys += [("concept", scope_key(student_id, "concept", course_id, cid)) for cid in ids]
    manifest, values = [], []
    for kind, key in keys:
        concept_id = next((cid for cid in ids if scope_key(student_id, "concept", course_id, cid) == key), "")
        state = ensure_scope_state(db, student_id, kind, course_id if kind != "student" else "", concept_id)
        row = db.get(ScopeMemory, key)
        item = {"student_id": student_id, "course_id": course_id, "scope_key": key,
            "scope_kind": kind, "invalidation_version": state.invalidation_version if state else 0,
            "enabled": state.enabled if state else True, "version": row.version if row else 0,
            "concept_id": next((cid for cid in ids if scope_key(student_id, "concept", course_id, cid) == key), "")
                if kind == "concept" else ""}
        manifest.append(item)
        if purpose in {"grading", "memory_extract"}:
            continue
        if row is None or not row.content or row.status != "active" or (state and not state.enabled):
            continue
        content, source_refs, _ = projected_fields(row)
        if not content:
            continue
        if state and (set(row.source_refs or []) & set(state.blocked_source_refs or [])
                      or {c.get("claim_id") for c in row.claims or []} & set(state.blocked_claim_ids or [])):
            continue
        # 正文采用代表性引用，完整来源及条目始终留在数据库按需下钻。
        values.append({"scope_key": key, "scope_kind": kind, "course_id": row.course_id,
            "concept_id": row.concept_id, "content": content, "version": row.version,
            "source_refs": source_refs[:3]})
    fingerprint_data = [{k: v for k, v in item.items() if k != "version"} for item in manifest]
    fingerprint = hashlib.sha256(json.dumps(fingerprint_data, sort_keys=True).encode()).hexdigest()

    def projection(selected):
        return {"global_memory": next((v for v in selected if v["scope_kind"] == "student"), None),
            "course_memory": next((v for v in selected if v["scope_kind"] == "course"), None),
            "concept_memories": [v for v in selected if v["scope_kind"] == "concept"],
            "memory_manifest": manifest, "memory_invalidation_fingerprint": fingerprint}

    # 优先保留当前知识点，再保留当前课程和总体；只移除完整可选单元，不截断正文。
    selected = list(values)
    result = projection(selected)
    while selected and estimate_tokens(json.dumps(result, ensure_ascii=False)) > budget:
        # 后续相关概念先移除，然后总体默认；当前概念有利于接续本次教学。
        remove = next((v for v in reversed(selected) if v["scope_kind"] == "concept" and v is not
                       next((c for c in selected if c["scope_kind"] == "concept"), None)), None)
        remove = remove or next((v for v in selected if v["scope_kind"] == "student"), None)
        remove = remove or next((v for v in selected if v["scope_kind"] == "course"), None) or selected[-1]
        selected.remove(remove)
        result = projection(selected)
    tokens = estimate_tokens(json.dumps(result, ensure_ascii=False))
    # 极小预算下manifest仍交给守卫，但不注入教学消息；调用方区分控制元数据与model_payload。
    model_payload = {key: result[key] for key in ("global_memory", "course_memory", "concept_memories")}
    if tokens > budget:
        model_payload = {}
    return {**result, "memory_input_tokens": tokens if tokens <= budget else 0,
        "memory_budget": budget, "model_payload": model_payload, "session_experiences": []}


def validate_manifest(db, manifest: list[dict]) -> bool:
    if not isinstance(manifest, list):
        return False
    for item in manifest:
        try:
            key, student = item["scope_key"], item["student_id"]
            kind, course = item["scope_kind"], item["course_id"]
            row = db.get(ScopeMemory, key)
            if row and (row.student_id != student or row.scope_kind != kind
                        or (kind != "student" and row.course_id != course)):
                return False
            if kind == "student" and key != scope_key(student, kind):
                return False
            if kind == "course" and key != scope_key(student, kind, course):
                return False
            if kind == "concept" and key != scope_key(student, kind, course, item["concept_id"]):
                return False
            state = ensure_scope_state(db, student, kind, course if kind != "student" else "", item.get("concept_id", ""))
            if (state.invalidation_version if state else 0) != item["invalidation_version"]:
                return False
            if (state.enabled if state else True) != item.get("enabled", True):
                return False
        except (KeyError, TypeError, ValueError):
            return False
    return True


def invalidation_epoch(db, student_id, *, lock=False):
    """撤销版本单调累加；新建零版本作用域不会让在途任务误失效。"""
    from app.domains.memory.scopes.models import SessionMemoryExtraction
    total = 0
    for model in (ScopeMemory, SessionMemoryExtraction):
        query = select(model).where(model.student_id == student_id)
        if lock:
            query = query.with_for_update()
        total += sum(row.invalidation_version for row in db.scalars(query))
    return total
