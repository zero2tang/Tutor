"""从可信控制行生成完整文档预写值；正式发布仍重新检查撤销与来源。"""

from sqlalchemy import select

from app.domains.memory.scopes.dependencies import allowed_sources, ensure_scope_state
from app.domains.memory.scopes.models import ScopeMemory, SessionConceptObservation, SessionMemoryExtraction
from app.infrastructure.persistence.memory_documents import (
    document_namespace, memory_document_repository, stage_memory_fields,
)


def _refs(claims):
    return sorted({ref for item in claims
                   for ref in [*item.get("source_refs", []), *item.get("counter_source_refs", [])]})


def _extraction_documents(db, snapshot, candidate):
    documents, affected = [], set()
    student, course, session_id = (snapshot[key] for key in ("student_id", "course_id", "session_id"))
    for item in candidate["observations"]:
        concept = item["concept_id"]
        scope = ensure_scope_state(db, student, "concept", course, concept)
        allowed = allowed_sources(db, scope, item["source_refs"])
        claims = [claim for claim in item["claims"] if set(_refs([claim])) <= allowed
                  and claim["claim_id"] not in (scope.blocked_claim_ids or [])]
        row = db.scalar(select(SessionConceptObservation).where(
            SessionConceptObservation.session_id == session_id, SessionConceptObservation.concept_id == concept))
        if row is None and not claims:
            continue
        fields = {"content": "\n".join(claim["text"] for claim in claims), "claims": claims, "source_refs": _refs(claims)}
        if row is None or any(getattr(row, key) != value for key, value in fields.items()):
            affected.add(concept)
        # 未插入的新观察仅提供服务器确认的归属，不在预写阶段发布SQL指针。
        identity = row if row is not None else SessionConceptObservation(
            session_id=session_id, student_id=student, course_id=course, concept_id=concept)
        documents.append((document_namespace(identity), fields))
    preferences = []
    for item in candidate["preferences"]:
        scope = ensure_scope_state(db, student, item["scope_kind"], course if item["scope_kind"] == "course" else "")
        if set(item["source_refs"]) <= allowed_sources(db, scope, item["source_refs"]):
            preferences.append(item)
    extraction = db.get(SessionMemoryExtraction, session_id)
    fields = {"content": candidate["experience"].get("content", ""),
        "experience_source_refs": candidate["experience"].get("source_refs", []),
        "concept_tags": sorted(affected | {item["concept_id"] for item in candidate["observations"] if item["claims"]}),
        "preferences": preferences, "source_refs": sorted({ref for item in preferences for ref in item["source_refs"]}),
        "unassigned": candidate["unassigned"]}
    documents.append((document_namespace(extraction), fields))
    return documents


async def stage_candidate_documents(store, runner, kind, snapshot, candidate):
    """短事务只读取控制与来源；所有候选Store I/O在事务结束后执行。"""
    if store is None:
        return
    database = runner.hierarchy.database if kind == "scope" else runner.service.database
    with database() as db:
        repository = memory_document_repository(db.get_bind())
        if kind == "extract":
            documents = _extraction_documents(db, snapshot, candidate)
        else:
            row = db.get(ScopeMemory, snapshot["scope_key"])
            fields = {"content": "\n".join(("- " if kind == "scope" else "") + item["text"] for item in candidate),
                "claims": candidate, "source_refs": _refs(candidate)}
            documents = [(document_namespace(row), fields)]
    for namespace, fields in documents:
        await stage_memory_fields(store, namespace, fields, repository=repository)
