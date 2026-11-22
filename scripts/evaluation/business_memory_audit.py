"""业务分层记忆验收：仅目录种子可写，学习记录和记忆统一只读审计。"""

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATABASE = "stem_tutor_business_memory_acceptance"


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def source_digest():
    value = sha256()
    for path in sorted((ROOT / "backend/app").rglob("*.py")):
        value.update(path.relative_to(ROOT).as_posix().encode())
        value.update(path.read_bytes())
    return value.hexdigest()


def seed_catalog(run_id: str, expected_database: str):
    """只写明示合成的课程、概念、题目；不伪造任何学生事实或模型记忆。"""
    from app.infrastructure.config import get_settings
    from app.infrastructure.persistence.database import db_session
    from app.domains.courses.models import Concept, Course, Question

    if expected_database != DATABASE or get_settings().database_name != DATABASE:
        raise ValueError("种子目录仅允许写入独立业务记忆验收数据库")
    algebra, physics = "bm_alg_" + run_id, "bm_phy_" + run_id
    concepts = {"distribution": algebra + ":distribution", "equality": algebra + ":equality",
        "unobserved": algebra + ":fraction", "motion": physics + ":motion"}
    questions = {"first": algebra + ":q1", "counter": algebra + ":q2"}
    criteria = [
        {"id": "expand", "concept_id": concepts["distribution"], "dimension": "advanced",
         "description": "按分配律展开括号，系数必须同时乘未知数项及常数项；第1问独立评分。"},
        {"id": "equality", "concept_id": concepts["equality"], "dimension": "advanced",
         "description": "为保持相等，两边须同时减去同一个数；第2问独立评分。"},
    ]
    with db_session() as db:
        for cid, title in ((algebra, "合成验收代数课"), (physics, "合成验收物理课")):
            if db.get(Course, cid) is not None:
                raise ValueError("本次合成目录ID已存在，拒绝覆盖")
            db.add(Course(id=cid, title=title, description="工程脚本创建的合成目录，不是真实教学实验。",
                source_ref="synthetic-business-memory-acceptance:" + run_id))
        for key, name in (("distribution", "分配律"), ("equality", "等式性质"), ("unobserved", "分数运算"), ("motion", "运动描述")):
            db.add(Concept(id=concepts[key], course_id=physics if key == "motion" else algebra,
                source_id=key, name=name, summary="合成工程验收概念", module="合成验收"))
        for key, stem, answer in (
            ("first", "【合成工程验收题】分别作答：第1问展开2(x+3)。第2问：由x+4=9，两边同减4应得到什么等式？分数运算仅是目录标签，本题没有第3问，不考查分数运算。", ["第1问2x+6；第2问x=5。"]),
            ("counter", "【合成工程验收的另一道题】分别作答：第1问展开3(x+2)。第2问：由x+5=11，两边同减5应得到什么等式？本题没有第3问，不考查分数运算。", ["第1问3x+6；第2问x=6。"]),
        ):
            db.add(Question(id=questions[key], course_id=algebra, source_id=key, stem=stem,
                options={}, answer=answer, question_type="fill", cognitive_type="understanding",
                concept_ids=[concepts["distribution"], concepts["equality"], concepts["unobserved"]],
                explanation="合成目录评分基准：括号各项同时乘系数；等式两边同时减去相同数。",
                source_ref="synthetic-business-memory-acceptance:" + run_id, generated=False,
                validation={"data_origin": "synthetic_engineering_fixture", "assessment_criteria": criteria,
                    "canonical_item_id": "synthetic:" + run_id + ":" + key}, version=1))
    return {"courses": {"algebra": algebra, "physics": physics}, "concepts": concepts,
        "questions": questions, "write_scope": "Course/Concept/Question only", "inserted_rows": 8}


def read_audit(student_id: str, session_ids: list[str]):
    """只读本次合成学生，保留正式评分、来源依赖和模型用量的可核查回执。"""
    from sqlalchemy import inspect, select
    from app.infrastructure.config import get_settings
    from app.infrastructure.persistence.database import db_session
    from app.infrastructure.persistence.execution_models import AgentRun
    from diagnostic_events import read_diagnostic_events
    from app.domains.assessment.models import Attempt
    from app.domains.sessions.models import LearningSession, Message
    from app.domains.students.models import StudentState
    from app.domains.memory.scopes.models import ScopeMemory, SessionConceptObservation
    from app.domains.assessment.models import EvidenceConceptLink
    from app.domains.memory.models import MemoryFact, MemoryJob
    from app.domains.memory.scopes.dependencies import scope_key

    def values(rows):
        return [{column.key: getattr(row, column.key) for column in inspect(type(row)).column_attrs} for row in rows]

    with db_session() as db:
        sessions = list(db.scalars(select(LearningSession).where(LearningSession.id.in_(session_ids),
            LearningSession.student_id == student_id)))
        owned = [row.id for row in sessions]
        if set(owned) != set(session_ids):
            raise ValueError("审计范围包含不属于本次学生的会话")
        attempts = list(db.scalars(select(Attempt).where(Attempt.session_id.in_(owned)).order_by(Attempt.created_at, Attempt.id)))
        states = list(db.scalars(select(StudentState).where(StudentState.student_id == student_id).order_by(StudentState.id)))
        links = list(db.scalars(select(EvidenceConceptLink).where(EvidenceConceptLink.student_id == student_id).order_by(EvidenceConceptLink.id)))
        messages = list(db.scalars(select(Message).where(Message.session_id.in_(owned)).order_by(Message.id)))
        facts = list(db.scalars(select(MemoryFact).where(MemoryFact.session_id.in_(owned)).order_by(MemoryFact.seq)))
        observations = list(db.scalars(select(SessionConceptObservation).where(SessionConceptObservation.student_id == student_id)))
        scopes = list(db.scalars(select(ScopeMemory).where(ScopeMemory.student_id == student_id)))
        # 尚未首次成功提交正文时也已存在scope job，不能仅从ScopeMemory找任务。
        keys = {row.scope_key for row in scopes} | {scope_key(student_id, "student")}
        for session in sessions:
            keys.add(scope_key(student_id, "course", session.course_id))
            keys.update(scope_key(student_id, "concept", session.course_id, cid) for cid in session.concept_ids)
        keys.update(scope_key(student_id, "concept", row.course_id, row.concept_id) for row in observations)
        scope_states = list(db.scalars(select(ScopeMemory).where(ScopeMemory.scope_key.in_(keys))))
        scope_jobs = list(db.scalars(select(MemoryJob).where(MemoryJob.scope_key.in_(keys))))
        jobs = list(db.scalars(select(MemoryJob).where(MemoryJob.job_kind == "session_extract", MemoryJob.session_id.in_(owned))))
        runs = list(db.scalars(select(AgentRun).where(AgentRun.session_id.in_(owned)).order_by(AgentRun.created_at)))
        events = read_diagnostic_events(owned)
        original = {model.__tablename__: {"count": len(rows), "sha256": digest(values(rows))}
            for model, rows in ((Attempt, attempts), (StudentState, states), (EvidenceConceptLink, links), (Message, messages))}
        return {"database": get_settings().database_name, "originals": original,
            "counts": {"attempts": len(attempts), "messages": len(messages), "facts": len(facts), "links": len(links), "runs": len(runs)},
            "attempts": [{"id": row.id, "session_id": row.session_id, "question_id": row.question_id,
                "answer_text": row.answer_text, "result": row.result, "assisted": row.assisted,
                "grading_packet": row.assessment.get("grading_packet"), "state_updates": row.state_updates} for row in attempts],
            "states": values(states), "links": values(links), "scopes": values(scopes),
            "scope_states": values(scope_states), "dependency_strategy": "fixed_scope_ancestors",
            "observations": values(observations),
            "facts": [{"id": row.id, "session_id": row.session_id, "source_ref": row.source_ref, "kind": row.kind,
                "seq": row.seq, "payload_sha256": digest(row.payload)} for row in facts],
            "jobs": values(jobs), "scope_jobs": values(scope_jobs),
            "runs": [{"id": row.id, "session_id": row.session_id, "turn_id": row.turn_id, "agent": row.agent, "skill": row.skill,
                "model": row.model, "status": row.status, "duration_ms": row.duration_ms,
                "prompt_tokens": row.prompt_tokens, "completion_tokens": row.completion_tokens,
                "tool_calls": len(row.tool_calls), "error": row.error} for row in runs],
            "events": [{"session_id": row.session_id, "event": row.event, "data": row.data} for row in events
                if row.event in {"context_purpose_selected", "memory_context_loaded", "model_request_completed", "error", "agent_failed", "context_budget"}],
            "fact_sources_unique": len({(row.session_id, row.source_ref) for row in facts}) == len(facts)}


def usage_totals(audit):
    jobs, scope_jobs, runs = audit.get("jobs", []), audit.get("scope_jobs", []), audit.get("runs", [])
    model_events = [item["data"] for item in audit.get("events", []) if item["event"] == "model_request_completed"]
    def total(items, key):
        return sum((item.get("metrics") or {}).get(key, 0) for item in items)
    return {"agent_runs": len(runs), "failed_agent_runs": sum(row["status"] != "completed" for row in runs),
        "agent_prompt_tokens_including_compaction": sum(row["prompt_tokens"] for row in runs),
        "agent_completion_tokens_including_compaction": sum(row["completion_tokens"] for row in runs),
        "leaf_memory_model_calls": total(jobs, "model_calls"), "scope_memory_model_calls": total(scope_jobs, "model_calls"),
        "memory_prompt_tokens": total(jobs + scope_jobs, "prompt_tokens"),
        "memory_completion_tokens": total(jobs + scope_jobs, "completion_tokens"),
        "memory_usage_unavailable_calls": total(jobs + scope_jobs, "usage_unavailable_calls"),
        "memory_job_attempts": sum(row["attempts"] for row in jobs + scope_jobs),
        "memory_status_counts": dict(Counter(row["status"] for row in jobs + scope_jobs)),
        "observed_agent_http_attempts": sum(row.get("request_attempts", 1) for row in model_events),
        "observed_agent_http_retries": sum(max(0, row.get("request_attempts", 1) - 1) for row in model_events),
        "billing_limit": "模型未返回usage的失败调用可能产生未恢复用量；不声称费用总账完整。"}


def latency_breakdown(audit):
    """正式评分与之后的自主接续分列，不为验收短路真实业务。"""
    grading_ids = {row["grading_packet"]["run_id"] for row in audit.get("attempts", []) if row.get("grading_packet")}
    grading = [row for row in audit.get("runs", []) if row["id"] in grading_ids]
    turns = {row["turn_id"] for row in grading}
    continuation = [row for row in audit.get("runs", []) if row.get("turn_id") in turns and row["agent"] == "supervisor"]
    def select(rows):
        return [{key: row[key] for key in ("id", "session_id", "duration_ms", "prompt_tokens", "completion_tokens", "status")} for row in rows]
    return {"grading_runs": select(grading), "post_assessment_supervisor_runs": select(continuation),
        "grading_duration_ms_sum": round(sum(row["duration_ms"] for row in grading), 2),
        "post_assessment_supervisor_duration_ms_sum": round(sum(row["duration_ms"] for row in continuation), 2),
        "note": "保留真实出题、评分和主Agent接续；AgentRun用量包含其上下文压缩。后台记忆可重叠，分项时间不作为并发吞吐或端到端可加总时间。"}
