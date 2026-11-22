"""业务分层压缩验收的只读审计；不读取参考解来编排模拟作答。"""

from collections import Counter
from hashlib import sha256

from memory_context_audit import digest_json, read_audit, source_digest, usage_totals


def public_probe_question(course_id):
    """仅选择已明确为合成目录的公开题干，不读取答案或解析来编排学生原话。"""
    from sqlalchemy import select
    from app.infrastructure.persistence.database import db_session
    from app.domains.courses.models import Question

    if not course_id.startswith("bm_alg_"):
        raise ValueError("普通待答压缩仅使用已声明的合成代数目录")
    with db_session() as db:
        row = db.execute(select(Question.id, Question.stem).where(Question.course_id == course_id)
                         .order_by(Question.id).limit(1)).one_or_none()
        if row is None:
            raise ValueError("合成目录中没有可冻结的真实题目")
        return {"id": row.id, "stem": row.stem}


def read_business_audit(student_id: str, session_ids: list[str]) -> dict:
    from sqlalchemy import select

    from app.infrastructure.persistence.database import db_session
    from app.domains.memory.models import MemoryJob
    from app.domains.memory.scopes.models import ScopeMemory
    from app.domains.sessions.models import LearningSession
    from app.domains.courses.models import Concept
    from app.domains.memory.scopes.dependencies import scope_key

    result = read_audit(student_id, session_ids)
    with db_session() as db:
        memories = list(db.scalars(select(ScopeMemory).where(ScopeMemory.student_id == student_id)))
        sessions = list(db.scalars(select(LearningSession).where(LearningSession.student_id == student_id,
                                                                LearningSession.id.in_(session_ids))))
        courses = {session.course_id for session in sessions}
        keys = {row.scope_key for row in memories} | {scope_key(student_id, "student")}
        keys.update(scope_key(student_id, "course", course) for course in courses)
        # 首次聚合失败时还没有正文，也必须计入任务、尝试次数及实际用量。
        keys.update(scope_key(student_id, "concept", concept.course_id, concept.id)
                    for concept in db.scalars(select(Concept).where(Concept.course_id.in_(courses))))
        jobs = list(db.scalars(select(MemoryJob).where(MemoryJob.scope_key.in_(keys)))) if keys else []
        result["scope_memories"] = [{"scope_key": row.scope_key, "scope_kind": row.scope_kind,
            "course_id": row.course_id, "concept_id": row.concept_id, "version": row.version,
            "status": row.status, "content_sha256": sha256(row.content.encode()).hexdigest(),
            "characters": len(row.content), "source_ref_count": len(row.source_refs),
            "invalidation_version": state.invalidation_version if (state := db.get(ScopeMemory, row.scope_key)) else 0}
            for row in memories]
        result["scope_jobs"] = [{"id": row.id, "scope_key": row.scope_key, "rule_version": row.rule_version,
            "status": row.status, "attempts": row.attempts, "error": row.error, "metrics": row.metrics,
            "source_generation": row.source_generation, "input_fingerprint": row.input_fingerprint}
            for row in jobs]
        result["response_snapshots"] = [{"session_id": session.id, "request_id": item.get("request_id"),
            "run_id": item.get("run_id"), "raw_input_sha256": sha256(item.get("raw_input", "").encode()).hexdigest(),
            "pending_question_before_sha256": sha256(item.get("pending_question_before", "").encode()).hexdigest(),
            "context": {key: (item.get("response_context") or {}).get(key) for key in (
                "workspace_id", "workspace_version", "policy_version", "problem_id", "problem_kind", "question_id",
                "response_expectation", "assisted", "help_level", "support_event_ids", "capture_method")},
            "question_sha256": sha256((item.get("response_context") or {}).get("question", "").encode()).hexdigest()}
            for session in sessions for item in (session.pending_context or {}).get("guided_teaching", {}).get("evidence", [])]
        result["pending_integrity"] = [{"session_id": session.id, "pending_question_id": session.pending_question_id,
            "contract_sha256": digest_json((session.pending_context or {}).get("contract")),
            "stem_sha256": sha256((session.pending_context or {}).get("contract", {}).get("stem", "").encode()).hexdigest(),
            "assisted": bool((session.pending_context or {}).get("assisted", False)),
            "cognitive_type": (session.pending_context or {}).get("cognitive_type")}
            for session in sessions]
    return result


def business_usage(audit: dict):
    totals = usage_totals(audit)
    jobs = audit.get("scope_jobs", [])
    totals.update(scope_job_count=len(jobs), scope_job_statuses=dict(Counter(job["status"] for job in jobs)),
        scope_model_calls=sum(job["metrics"].get("model_calls", 0) for job in jobs),
        scope_aggregation_calls=sum(job["metrics"].get("aggregation_calls", 0) for job in jobs),
        promotion_review_calls=sum(job["metrics"].get("review_calls", 0) for job in jobs),
        scope_prompt_tokens=sum(job["metrics"].get("prompt_tokens", 0) for job in jobs),
        scope_completion_tokens=sum(job["metrics"].get("completion_tokens", 0) for job in jobs),
        scope_usage_unavailable_calls=sum(job["metrics"].get("usage_unavailable_calls", 0) for job in jobs),
        teaching_call_events=sum(event["event"] == "model_request_completed" for event in audit["events"]),
        summary_model_calls_lower_bound=len(audit["checkpoints"]),
        summary_exact_call_count_available=False,
        cost_scope="AgentRun含摘要Token；MemoryJob只含叶子；MemoryJob含概念/上层/审查。摘要分批与失败调用未单独逐次发事件，调用次数只报下界，不据此声明成本总账完整。")
    return totals
