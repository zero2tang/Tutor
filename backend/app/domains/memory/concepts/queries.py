"""当前目标知识点的只读下钻；身份来自服务端会话，不接受模型传入。"""

from app.domains.memory.scopes.models import SessionConceptObservation
from sqlalchemy import select

from app.infrastructure.persistence.database import db_session
from app.domains.memory.scopes.dependencies import allowed_sources, ensure_scope_state
from app.domains.sessions.models import LearningSession


def concept_memory_query(student, context, concept_id: str, *, observations=False, limit=3):
    # 与运行器冻结的三个知识点记忆清单一致，撤销守卫可覆盖工具下钻结果。
    if concept_id not in list(dict.fromkeys(context.concept_ids))[:3]:
        raise PermissionError("只能读取当前三个教学目标内的知识点记忆")
    with db_session() as db:
        session = db.get(LearningSession, context.session_id)
        if session is None or session.student_id != context.student_id or session.course_id != context.course_id:
            raise PermissionError("知识点记忆读取范围与当前学习会话不一致")
        if observations:
            state = ensure_scope_state(db, context.student_id, "concept", context.course_id, concept_id)
            rows = db.scalars(select(SessionConceptObservation).where(
                SessionConceptObservation.student_id == context.student_id,
                SessionConceptObservation.course_id == context.course_id,
                SessionConceptObservation.concept_id == concept_id,
                SessionConceptObservation.status == "active",
            ).order_by(SessionConceptObservation.updated_at.desc()).limit(limit)).all()
            items = []
            for row in rows:
                refs = set(row.source_refs or [])
                if state and (not state.enabled or allowed_sources(db, state, refs) != refs):
                    continue
                items.append({"observation_id": row.id, "session_id": row.session_id,
                    "concept_id": concept_id, "content": row.content, "version": row.version,
                    "source_refs": list(row.source_refs or []), "claims": row.claims})
            return {"concept_id": concept_id, "observations": items,
                "notice": "会话观察不等于正式掌握度，辅助条件与反例见原始来源。"}
    profile = student.profile(context.course_id, [concept_id], context.student_id)
    return {"concept_id": concept_id, "state": next(iter(profile["states"]), None),
        "concept_memories": profile.get("concept_memories", []),
        "notice": "正式评估状态与模型提取的教学观察分别展示，不能相互替代。"}
