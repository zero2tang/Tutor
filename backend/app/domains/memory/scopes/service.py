"""课程与总体记忆的按需聚合、短事务提交及作用域维护入口。"""

from app.domains.memory.scopes.models import (
    ScopeMemory,
    SessionConceptObservation,
    SessionMemoryExtraction,
)
from app.domains.memory.models import MemoryJob

import asyncio
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from sqlalchemy import case, delete, func, select, update

from app.infrastructure.persistence.database import db_session
from app.domains.memory.scopes.dependencies import (
    allowed_sources,
    ensure_scope_state,
    scope_key,
    source_facts,
)
from app.domains.memory.scopes.invalidation import bump_scope, invalidate_dependents
from app.domains.memory.scopes.promotion import AGGREGATION_RULE, aggregate_candidate, empty_metrics
from app.domains.memory.scopes.preferences import preference_index, valid_upper_claims
from app.domains.memory.schemas import estimate_tokens
from app.domains.memory.context import memory_value, read_context, validate_manifest
from app.domains.courses.models import Concept
from app.infrastructure.persistence.defaults import now
from app.domains.sessions.models import LearningSession
from app.domains.memory.models import MemoryFact


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def input_fingerprint(children):
    """偏好无变化的会话水位推进不触发上层再摘要；真实依赖版本仍独立CAS。"""
    values = [{"kind": child["kind"], "key": child["key"],
               "version": child.get("semantic_version", child["version"])} for child in children]
    return fingerprint(sorted(values, key=lambda child: (child["kind"], child["key"])))


class ScopeMemoryConflict(RuntimeError):
    """聚合期间来源集合、依赖版本或撤销屏障发生变化。"""


class HierarchicalMemoryService:
    def __init__(self, models, database=None, review_models=None):
        self.models = models
        self.review_models = review_models or models
        self.database = database or db_session
        self._lock = asyncio.Lock()

    @staticmethod
    def _session(db, session_id):
        session = db.get(LearningSession, session_id)
        if session is None:
            raise ValueError("会话不存在")
        return session

    @staticmethod
    def _scope(db, session, scope, concept_id=None):
        scope = "student" if scope == "global" else scope
        course = session.course_id if scope != "student" else ""
        concept = concept_id or "" if scope == "concept" else ""
        if scope == "concept":
            row = db.get(Concept, concept)
            if row is None or row.course_id != session.course_id:
                raise ValueError("知识点不属于当前课程")
        return scope, course, concept, scope_key(session.student_id, scope, course, concept)

    @staticmethod
    def _row(db, student, kind, course="", concept=""):
        key = scope_key(student, kind, course, concept)
        row = db.get(ScopeMemory, key)
        if row is None:
            row = ScopeMemory(scope_key=key, student_id=student, scope_kind=kind, course_id=course,
                concept_id=concept, content="", claims=[], source_refs=[], version=0,
                source_generation=0, status="active", dependency_fingerprint="")
            db.add(row)
            db.flush()
        return row

    def _inputs(self, db, student, kind, course):
        child_kind = "concept" if kind == "course" else "course"
        query = select(ScopeMemory).where(ScopeMemory.student_id == student,
            ScopeMemory.scope_kind == child_kind, ScopeMemory.status == "active")
        if course:
            query = query.where(ScopeMemory.course_id == course)
        children = []
        for row in db.scalars(
            query.order_by(ScopeMemory.updated_at.desc(), ScopeMemory.scope_key)
        ):
            state = db.get(ScopeMemory, row.scope_key)
            if not row.content or (state and not state.enabled):
                continue
            claims = valid_upper_claims(row)
            if kind == "student":
                # 课程显式偏好不作为总体能力模式输入；总体偏好只走其独立原子来源。
                claims = [claim for claim in claims if not claim.get("explicit_preference")]
            if not claims:
                continue
            refs = sorted({ref for claim in claims for ref in [*claim.get("source_refs", []), *claim.get("counter_source_refs", [])]})
            children.append({"kind": "scope", "key": row.scope_key, "version": row.version,
                "source_refs": refs, "claims": claims,
                "course_id": row.course_id, "concept_id": row.concept_id})
        query = select(SessionMemoryExtraction).where(SessionMemoryExtraction.student_id == student)
        if course:
            query = query.where(SessionMemoryExtraction.course_id == course)
        preferences, extraction_children = [], []
        for row in db.scalars(query.order_by(SessionMemoryExtraction.updated_at.desc())):
            found = [p for p in row.preferences or [] if p.get("scope_kind") == kind and p.get("explicit") is True]
            if found:
                preferences.extend(found)
                extraction_children.append({"kind": "extraction", "key": row.session_id,
                    "version": row.version, "semantic_version": fingerprint(found),
                    "source_refs": sorted({ref for p in found for ref in p.get("source_refs", [])})})
        return children, preferences, extraction_children

    def _snapshot(self, db, student, kind, course):
        state = ensure_scope_state(db, student, kind, course)
        row = self._row(db, student, kind, course)
        if not state.enabled:
            for old_job in db.scalars(
                select(MemoryJob).where(
                    MemoryJob.scope_key == row.scope_key,
                    MemoryJob.rule_version == AGGREGATION_RULE,
                    MemoryJob.status.in_(["pending", "retry"]),
                )
            ):
                old_job.status, old_job.lease_until = "superseded", None
            return None
        children, preferences, extraction_children = self._inputs(db, student, kind, course)
        input_set = sorted([{key: child[key] for key in ("kind", "key", "version")}
            for child in children + extraction_children], key=lambda child: (child["kind"], child["key"]))
        input_fp = input_fingerprint(children + extraction_children)
        obsolete = db.scalars(
            select(MemoryJob).where(
                MemoryJob.scope_key == row.scope_key,
                MemoryJob.rule_version == AGGREGATION_RULE,
                MemoryJob.status.in_(["pending", "retry"]),
            )
        ).all()
        for old_job in obsolete:
            if old_job.source_generation != state.generation or old_job.input_fingerprint != input_fp:
                old_job.status, old_job.lease_until = "superseded", None
        # 相同输入已经处理过，不登记新任务，也不重复调用模型。
        prior = db.scalar(
            select(MemoryJob)
            .where(
                MemoryJob.scope_key == row.scope_key,
                MemoryJob.input_fingerprint == input_fp,
                MemoryJob.rule_version == AGGREGATION_RULE,
                MemoryJob.status.in_(["completed", "running"]),
            )
            .order_by(MemoryJob.created_at.desc())
        )
        if (prior and row.status == "active") or (not input_set and not row.content):
            for old_job in obsolete:
                if old_job.status in {"pending", "retry"}:
                    old_job.status, old_job.lease_until = "superseded", None
            return None
        previous = db.scalar(
            select(MemoryJob).where(
                MemoryJob.scope_key == row.scope_key,
                MemoryJob.source_generation == state.generation,
                MemoryJob.rule_version == AGGREGATION_RULE,
            )
        )
        if previous and previous.input_fingerprint and previous.input_fingerprint != input_fp:
            state = bump_scope(db, row.scope_key)
            previous = None
        job = previous
        if job and job.attempts >= 3:
            if job.status not in {"completed", "superseded"}:
                job.status = "failed"
            return None
        if job is None:
            job = MemoryJob(
                id=str(uuid4()),
                job_kind="scope_aggregate",
                scope_key=row.scope_key,
                source_generation=state.generation,
                rule_version=AGGREGATION_RULE,
                input_fingerprint=input_fp,
                status="pending",
                attempts=0,
                metrics={},
                result_snapshot={},
            )
            db.add(job)
        elif job.available_at and job.available_at > now():
            return None
        job.input_fingerprint = input_fp
        job.status, job.error, job.attempts = "running", None, job.attempts + 1
        job.lease_until = now() + timedelta(minutes=5)
        job.lease_token = uuid4().hex
        job.result_snapshot = {**(job.result_snapshot or {}), "owner": {"student_id": student, "scope_kind": kind, "course_id": course},
                               "input_fingerprint": input_fp}
        selected = children[:8]
        preferences = preferences[:8]
        old_claims = valid_upper_claims(row) if row.status == "active" else []
        refs = {ref for child in selected for ref in child["source_refs"]}
        refs.update(ref for p in preferences for ref in p.get("source_refs", []))
        refs.update(ref for c in old_claims for ref in [*c.get("source_refs", []), *c.get("counter_source_refs", [])])
        valid = allowed_sources(db, state, refs)
        facts = source_facts(db, student, valid, course if kind == "course" else None)
        # 直接偏好必须可回到学生原话，而不能引用助手总结或另一位学生的表达。
        preferences = [p for p in preferences if set(p.get("source_refs", [])) <= set(facts)
            and all(facts[r]["kind"] in {"message", "correction"}
                and (facts[r]["kind"] == "correction" or facts[r]["payload"].get("role") == "user")
                for r in p.get("source_refs", []))]
        for child in selected:
            for claim in child["claims"]:
                for ref in claim.get("source_refs", []):
                    if ref in facts:
                        facts[ref].setdefault("concept_ids", [])
                        facts[ref]["concept_ids"] = list(set(facts[ref]["concept_ids"]) |
                            set(claim.get("concept_ids", [])) | ({child["concept_id"]} if child["concept_id"] else set()))
        behavioral_refs = {ref for child in selected for claim in child["claims"]
            if not claim.get("explicit_preference") for ref in claim.get("source_refs", [])}
        for ref, fact in facts.items():
            fact["preference_only"] = ref not in behavioral_refs
        pref_index = preference_index(preferences, kind)
        old_claims = [c for c in old_claims if c.get("claim_id") not in state.blocked_claim_ids
            and set([*c.get("source_refs", []), *c.get("counter_source_refs", [])]) <= set(facts)]
        old_claims = [c for c in old_claims if not c.get("explicit_preference")
            or (c.get("preference_id") in pref_index and c.get("text") == pref_index[c["preference_id"]]["text"])]
        model_facts = deepcopy(facts)
        for fact in model_facts.values():
            payload = fact.pop("payload")
            authorized_quotes = [quote for preference in preferences for quote in preference.get("source_quotes", [])
                                 if quote.get("source_ref") == fact["source_ref"]]
            # 同一原消息可含多个作用域；偏好证据只暴露本scope已校验的精确子句。
            raw = json.dumps({"scope_kind": kind, "authorized_quotes": authorized_quotes}
                if fact.get("preference_only") else payload, ensure_ascii=False)
            fact["source_excerpt"] = {"text": raw[:600], "start": 0, "end": min(600, len(raw)),
                "total_chars": len(raw), "partial": len(raw) > 600}
        model_children = []
        for child in selected:
            # 父作用域遗忘后，子记忆在原作用域仍有效；不能把父层禁止的条目继续给父层模型。
            claims = [claim for claim in child["claims"]
                if set([*claim.get("source_refs", []), *claim.get("counter_source_refs", [])]) <= set(facts)]
            if claims:
                model_children.append({"course_id": child["course_id"], "concept_id": child["concept_id"],
                                       "claims": claims[:3]})
        snapshot = {"scope_kind": kind, "course_id": course, "old_claims": old_claims,
            "children": model_children, "preferences": preferences, "facts": model_facts}
        # 输入是有位置的证据摘录；缩短摘录不改写原始事实或完整持久依赖。
        while estimate_tokens(json.dumps(snapshot, ensure_ascii=False)) > 10000:
            changed = False
            for fact in model_facts.values():
                excerpt = fact["source_excerpt"]
                if len(excerpt["text"]) > 100:
                    excerpt["text"] = excerpt["text"][:max(100, len(excerpt["text"]) // 2)]
                    excerpt["end"], excerpt["partial"] = len(excerpt["text"]), True
                    changed = True
            if not changed:
                raise ValueError("分层记忆固定引用超过输入预算")
        return {
            "job_id": job.id,
            "student": student,
            "kind": kind,
            "course": course,
            "scope_key": row.scope_key,
            "version": row.version,
            "generation": state.generation,
            "invalidation_version": state.invalidation_version,
            "input_fingerprint": input_fp,
            "children": children + extraction_children,
            "model": snapshot,
            "lease_token": job.lease_token,
        }

    async def _aggregate_scope(self, student, kind, course=""):
        with self.database() as db:
            snapshot = self._snapshot(db, student, kind, course)
        if snapshot is None:
            return {"scope_kind": kind, "status": "unchanged", "model_calls": 0}
        from app.domains.memory.workflow import MemoryGraphRunner
        owner = getattr(self, "queue_service", None)
        if owner is not None:
            runner = owner.memory_graph
        else:
            if not hasattr(self, "_memory_graph"):
                self._memory_graph = MemoryGraphRunner(hierarchy=self)
            runner = self._memory_graph
        try:
            return await runner.run("scope", snapshot)
        except Exception as exc:
            result = self.fail_scope(snapshot, exc, runner.failure_metrics(snapshot["job_id"]),
                accounting=runner.failure_accounting(snapshot["job_id"]))
            if isinstance(exc, ScopeMemoryConflict):
                return result
            raise

    def publish_scope(self, snapshot, claims, audit, metrics):
        student, kind, course = (snapshot[key] for key in ("student", "kind", "course"))
        with self.database() as db:
            state = db.get(ScopeMemory, snapshot["scope_key"], with_for_update=True)
            task = db.get(MemoryJob, snapshot["job_id"], with_for_update=True)
            if task.status == "completed":
                return {"scope_kind": kind, "status": "completed", "job_id": task.id, "metrics": task.metrics}
            if (
                task.status != "running"
                or task.lease_token != snapshot["lease_token"]
                or task.lease_until is None
                or task.lease_until <= now()
            ):
                raise ScopeMemoryConflict("聚合租约已失效")
            row = db.get(ScopeMemory, snapshot["scope_key"])
            children, _, extra = self._inputs(db, student, kind, course)
            current_fp = input_fingerprint(children + extra)
            versions = {(c["kind"], c["key"]): c["version"] for c in children + extra}
            old_versions = {(c["kind"], c["key"]): c["version"] for c in snapshot["children"]}
            if (state.generation != snapshot["generation"] or state.invalidation_version != snapshot["invalidation_version"]
                or not state.enabled or row.version != snapshot["version"] or current_fp != snapshot["input_fingerprint"]
                or versions != old_versions):
                raise ScopeMemoryConflict("聚合输入或撤销代次已变化")
            content = "\n".join("- " + claim["text"] for claim in claims)
            refs = sorted({ref for claim in claims for ref in [*claim.get("source_refs", []), *claim.get("counter_source_refs", [])]})
            changed = row.content != content or row.claims != claims or row.source_refs != refs or row.status != "active"
            values = dict(content=content, claims=claims, source_refs=refs, status="active",
                version=row.version + int(changed), source_generation=state.generation,
                dependency_fingerprint=snapshot["input_fingerprint"], updated_at=now())
            # 行锁与版本核验后走ORM属性，让Store文档适配器发布正文。
            for key, value in values.items():
                setattr(row, key, value)
            job = db.get(MemoryJob, snapshot["job_id"])
            job.status, job.error, job.metrics, job.lease_until = "completed", None, self._merge(job.metrics, metrics), None
            job.result_snapshot = {**job.result_snapshot, "review": audit, "version": values["version"], "changed": changed}
            if changed and kind == "course":
                parent = ensure_scope_state(db, student, "student")
                bump_scope(db, parent.scope_key)
                from app.domains.memory.recording.jobs import enqueue_scope
                enqueue_scope(db, parent, AGGREGATION_RULE)
            return {"scope_kind": kind, "status": "completed", "changed": changed,
                "job_id": job.id, "metrics": metrics}

    def fail_scope(self, snapshot, exc, metrics, *, accounting=None):
        with self.database() as db:
            job = db.get(MemoryJob, snapshot["job_id"])
            if (
                job.lease_token != snapshot["lease_token"]
                or job.status != "running"
            ):
                return
            job.status = "superseded" if isinstance(exc, ScopeMemoryConflict) else "retry" if job.attempts < 3 else "failed"
            job.error, job.metrics = type(exc).__name__, self._merge(job.metrics, metrics)
            job.result_snapshot = {**job.result_snapshot, "graph_usage_accounted": accounting or {}}
            if isinstance(exc, ValueError):
                # 固定校验信息不含原始学生文本、模型全文或数据库参数，便于故障定位。
                reason = str(exc)
                known = {
                    "上层候选全部拒绝且无有效旧条目": "all_candidates_rejected",
                    "上层记忆正文过长或引用了未知来源": "candidate_length_or_source_invalid",
                    "上层记忆条目格式或数量不合法": "candidate_shape_invalid",
                    "未知条目认识状态": "epistemic_status_invalid",
                    "上层记忆正文超过800估算Tokens": "body_budget_exceeded",
                    "晋升审查引用了未知候选": "review_candidate_id_invalid",
                    "晋升审查对同一条目返回冲突决定": "review_duplicate_decision",
                }
                job.result_snapshot = {**job.result_snapshot, "failure_reason_code": known.get(reason, "candidate_validation_failed")}
            job.lease_until = None
            job.available_at = now() + timedelta(seconds=min(60, 5 * 2 ** max(0, job.attempts - 1)))
        if isinstance(exc, ScopeMemoryConflict):
            return {"scope_kind": snapshot["kind"], "status": "superseded", "metrics": metrics}
        return None


    @staticmethod
    def _merge(old, new):
        result = dict(old or {})
        for key, value in new.items():
            result[key] = round(result.get(key, 0) + value, 2)
        return result

    async def aggregate(self, session_id: str):
        async with self._lock:
            with self.database() as db:
                session = self._session(db, session_id)
                student, course = session.student_id, session.course_id
            course_result = await self._aggregate_scope(student, "course", course)
            student_result = await self._aggregate_scope(student, "student")
            return {"course": course_result, "student": student_result}

    def read(self, session_id: str):
        with self.database() as db:
            session = self._session(db, session_id)
            ensure_scope_state(db, session.student_id, "student")
            ensure_scope_state(db, session.student_id, "course", session.course_id)
            concepts = list(db.scalars(select(ScopeMemory).where(ScopeMemory.student_id == session.student_id,
                ScopeMemory.course_id == session.course_id, ScopeMemory.scope_kind == "concept")))
            observations = list(db.scalars(select(SessionConceptObservation).where(
                SessionConceptObservation.session_id == session_id)))
            catalog_ids = set(db.scalars(select(Concept.id).where(Concept.course_id == session.course_id)))
            local_ids = ({row.concept_id for row in concepts} | {row.concept_id for row in observations}
                         | set(session.concept_ids or [])) & catalog_ids
            for concept_id in local_ids:
                ensure_scope_state(db, session.student_id, "concept", session.course_id, concept_id)
            keys = [scope_key(session.student_id, "student"), scope_key(session.student_id, "course", session.course_id),
                    *[scope_key(session.student_id, "concept", session.course_id, cid) for cid in catalog_ids]]
            # 首次聚合失败时尚无ScopeMemory，仍须按归属目录显示该scope任务。
            counts = dict(
                db.execute(
                    select(MemoryJob.status, func.count())
                    .where(MemoryJob.scope_key.in_(keys))
                    .group_by(MemoryJob.status)
                ).all()
            )
            jobs = list(
                db.scalars(
                    select(MemoryJob)
                    .where(MemoryJob.scope_key.in_(keys))
                    .order_by(
                        case(
                            (
                                MemoryJob.status.in_(
                                    ["failed", "retry", "running", "pending"]
                                ),
                                0,
                            ),
                            else_=1,
                        ),
                        MemoryJob.created_at.desc(),
                    )
                    .limit(20)
                )
            )

            def visible(row):
                if row is None:
                    return None
                state = db.get(ScopeMemory, row.scope_key)
                result = memory_value(row)
                result["enabled"] = state.enabled if state else True
                result["invalidation_version"] = state.invalidation_version if state else 0
                refs = set(row.source_refs or [])
                if row.status != "active" or (state and (not state.enabled or allowed_sources(db, state, refs) != refs)):
                    result.update(content="", claims=[], source_refs=[])
                return result

            visible_observations = []
            for observation in observations:
                state = db.get(
                    ScopeMemory,
                    scope_key(
                        session.student_id,
                        "concept",
                        session.course_id,
                        observation.concept_id,
                    ),
                )
                refs = set(observation.source_refs or [])
                if (observation.status != "active" or observation.concept_id not in catalog_ids
                    or (state and (not state.enabled or allowed_sources(db, state, refs) != refs))):
                    continue
                visible_observations.append({"id": observation.id, "concept_id": observation.concept_id,
                    "content": observation.content, "claims": observation.claims,
                    "source_refs": observation.source_refs, "version": observation.version,
                    "covered_seq": observation.covered_seq, "status": observation.status})
            return {"global_memory": visible(db.get(ScopeMemory, keys[0])),
                "course_memory": visible(db.get(ScopeMemory, keys[1])),
                "concept_memories": [visible(row) for row in concepts],
                "concept_observations": visible_observations,
                "scope_jobs": [{"id": job.id, "scope_key": job.scope_key, "source_generation": job.source_generation,
                    "status": job.status, "attempts": job.attempts, "error": job.error, "metrics": job.metrics} for job in jobs],
                "scope_job_summary": {"total": sum(counts.values()), "returned": len(jobs), "statuses": counts}}

    def context_for_scope(self, student_id, course_id, concept_ids=None, limit=3,
                          input_budget=20000, purpose="teaching", exclude_session_id=None):
        with self.database() as db:
            return read_context(db, student_id, course_id, concept_ids, limit=limit,
                                input_budget=input_budget, purpose=purpose)

    def context_for_session(self, session_id, concept_ids=None, limit=3, input_budget=20000, purpose="teaching"):
        with self.database() as db:
            session = self._session(db, session_id)
            return read_context(db, session.student_id, session.course_id,
                concept_ids if concept_ids is not None else session.concept_ids, limit=limit,
                input_budget=input_budget, purpose=purpose)

    def validate_context(self, manifest):
        with self.database() as db:
            return validate_manifest(db, manifest)

    def forget(self, session_id, scope="student", concept_id=None, claim_id=None, source_refs=None, disable=False):
        with self.database() as db:
            session = self._session(db, session_id)
            kind, course, concept, key = self._scope(db, session, scope, concept_id)
            state = ensure_scope_state(db, session.student_id, kind, course, concept)
            row = self._row(db, session.student_id, kind, course, concept)
            refs = set(source_refs or [])
            if claim_id:
                claim = next((c for c in row.claims if c.get("claim_id") == claim_id), None)
                if claim is None:
                    raise ValueError("条目不属于指定作用域")
                refs.update([*claim.get("source_refs", []), *claim.get("counter_source_refs", [])])
                state.blocked_claim_ids = list(set(state.blocked_claim_ids or []) | {claim_id})
            if source_refs and set(source_refs) - set(row.source_refs):
                raise ValueError("待撤销来源不属于指定作用域")
            whole_scope = not refs
            if whole_scope:
                refs = set(row.source_refs)
                state.cutoff_at = now()
            state.blocked_source_refs = sorted(set(state.blocked_source_refs or []) | refs)
            if disable:
                state.enabled = False
            bump_scope(db, key, invalidate=True)
            retained = [c for c in row.claims if not set([*c.get("source_refs", []), *c.get("counter_source_refs", [])]) & refs]
            if whole_scope or disable:
                retained = []
            row.claims, row.content = retained, "\n".join("- " + c["text"] for c in retained)
            row.source_refs = sorted({r for c in retained for r in [*c.get("source_refs", []), *c.get("counter_source_refs", [])]})
            row.version += 1
            row.status = "active"
            if kind == "concept":
                for observation in db.scalars(select(SessionConceptObservation).where(
                    SessionConceptObservation.student_id == session.student_id,
                    SessionConceptObservation.course_id == course, SessionConceptObservation.concept_id == concept)):
                    if whole_scope or set(observation.source_refs) & refs:
                        observation.status = "stale"
                        observation.invalidation_version += 1
                        invalidate_dependents(db, "observation", observation.id, refs)
            affected = invalidate_dependents(db, "scope", key, refs)
            return {"scope": kind, "scope_key": key, "invalidation_version": state.invalidation_version,
                "enabled": state.enabled, "affected_scope_keys": sorted(affected), "raw_evidence_preserved": True}

    def set_enabled(self, session_id, enabled, scope="student", concept_id=None):
        with self.database() as db:
            session = self._session(db, session_id)
            kind, course, concept, key = self._scope(db, session, scope, concept_id)
            state = ensure_scope_state(db, session.student_id, kind, course, concept)
            if state.enabled == bool(enabled):
                return {"scope": kind, "scope_key": key, "enabled": state.enabled,
                        "invalidation_version": state.invalidation_version}
            if enabled:
                # 暂停记录与忘记历史分开：保留暂停前正文，阻止暂停期间的事实在恢复后回填。
                query = select(MemoryFact).join(LearningSession,
                    LearningSession.id == MemoryFact.session_id).where(LearningSession.student_id == session.student_id)
                if course:
                    query = query.where(LearningSession.course_id == course)
                paused_at = state.updated_at.isoformat()
                blocked = {fact.source_ref for fact in db.scalars(query)
                    if str(fact.payload.get("_source_created_at", fact.created_at.isoformat())) >= paused_at}
                state.blocked_source_refs = sorted(set(state.blocked_source_refs or []) | blocked)
            state.enabled = bool(enabled)
            bump_scope(db, key, invalidate=True)
            affected = invalidate_dependents(db, "scope", key)
            return {"scope": kind, "scope_key": key, "enabled": state.enabled,
                    "invalidation_version": state.invalidation_version,
                    "affected_scope_keys": sorted(affected)}

    async def correct(self, session_id, text, scope="student", concept_id=None, claim_id=None, wait=True):
        if not text.strip() or estimate_tokens(text) > 2000:
            raise ValueError("纠正内容须非空且不超过2000估算Tokens")
        result = self.forget(session_id, scope, concept_id, claim_id)
        # 修正必须重新走事实提取；本入口只登记有作用域的明确原始请求，不直接写正文。
        from app.domains.memory.recording.facts import record_fact
        with self.database() as db:
            session = self._session(db, session_id)
            kind, course, concept, _ = self._scope(db, session, scope, concept_id)
            ref = "correction:" + str(uuid4())
            record_fact(db, session_id, ref, "correction", {"instruction": text.strip(),
                "scope_kind": kind, "course_id": course, "concept_id": concept,
                "explicit_student_request": True, "kind": kind})
        return {**result, "correction_source_ref": ref, "status": "recorded", "requires_extraction": True}
