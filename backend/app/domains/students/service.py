"""证据、诊断估计和偏好记忆分别维护。"""

from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence
from sqlalchemy import select

from app.infrastructure.persistence.database import db_session
from app.domains.assessment.models import Attempt
from app.domains.courses.models import Concept
from app.domains.students.models import MemoryItem, StudentState
from app.domains.students.mastery import MasteryEvidence, apply_ledger_evidence
from app.domains.courses.catalog import CatalogService

LEVELS = ["beginner", "advanced", "proficient"]


class StudentService:
    def profile(
        self,
        course_id: str = "mechanics",
        concept_ids: list[str] | None = None,
        student_id: str = "local",
    ) -> dict:
        """按课程组装学生学习视图：知识点状态、作答统计与有效记忆。

        没有独立证据的知识点也会返回，但 mastery 为 null、状态为未测，
        不能把目录中存在一个知识点等同于学生已经学习或作答。
        """
        CatalogService().course(course_id)
        with db_session() as db:
            concepts = {
                c.id: c
                for c in db.scalars(
                    select(Concept).where(Concept.course_id == course_id)
                )
            }
            if concept_ids:
                if set(concept_ids) - concepts.keys():
                    raise ValueError("学习状态目标包含未知或其他课程知识点")
                concepts = {
                    key: value for key, value in concepts.items() if key in concept_ids
                }
            states = {
                s.concept_id: s
                for s in db.scalars(
                    select(StudentState).where(StudentState.student_id == student_id)
                )
                if s.concept_id in concepts and s.evidence_ids
            }
            items = []
            for cid, concept in concepts.items():
                state = states.get(cid)
                mastery = state.mastery if state else dict.fromkeys(LEVELS)
                items.append(
                    {
                        "concept_id": cid,
                        "name": concept.name,
                        "mastery": mastery,
                        "evidence_count": len(state.evidence_questions) if state else 0,
                        "version": state.version if state else 0,
                        "evidence_ids": list(state.evidence_ids) if state else [],
                        "updated_at": state.updated_at.isoformat() if state else None,
                        "misconceptions": state.misconceptions if state else [],
                        "status": (
                            "mastered"
                            if state
                            and all(
                                v is not None and v >= 0.9 for v in mastery.values()
                            )
                            else "learning" if state else "unassessed"
                        ),
                        "next_review_at": (
                            state.next_review_at.isoformat()
                            if state and state.next_review_at
                            else None
                        ),
                    }
                )
            from app.domains.sessions.models import LearningSession

            attempts = db.scalars(
                select(Attempt)
                .join(LearningSession, Attempt.session_id == LearningSession.id)
                .where(
                    LearningSession.student_id == student_id,
                    LearningSession.course_id == course_id,
                )
                .order_by(Attempt.created_at.desc())
            ).all()
            memories = db.scalars(
                select(MemoryItem)
                .where(
                    MemoryItem.student_id == student_id,
                    MemoryItem.course_id == course_id,
                    MemoryItem.status == "active",
                )
                .order_by(MemoryItem.created_at.desc())
                .limit(20)
            ).all()
            result = {
                "student_id": student_id,
                "course_id": course_id,
                "states": items,
                "assessed_concepts": len(states),
                "attempt_count": len(attempts),
                "correct_count": sum(a.result == "correct" for a in attempts),
                "memories": [
                    {
                        "id": m.id,
                        "kind": m.kind,
                        "text": m.text,
                        "concept_id": m.concept_id,
                        "evidence_ids": m.evidence_ids,
                    }
                    for m in memories
                ],
            }
        memory = getattr(self, "memory_service", None)
        if memory is not None:
            scoped = memory.context_for_scope(student_id, course_id, concept_ids)
            # 运行器与页面分别读取投影；控制清单和原始快照不重复进入模型输入。
            result.update(scoped.get("model_payload", scoped))
            if getattr(memory, "business_pipeline", None) is not None:
                result["memory_schema"] = "business-memory-v2"
                result["session_experiences"] = []
            result["memories"] = []
        return result

    def memory(
        self, course_id: str, concept_ids: list[str], student_id: str = "local"
    ) -> list[dict]:
        """从最近有效记忆中选出当前知识点或课程级记忆，最多返回 8 条供教学使用。"""
        memory = getattr(self, "memory_service", None)
        if memory is not None:
            context = memory.context_for_scope(student_id, course_id, concept_ids)
            if getattr(memory, "business_pipeline", None) is not None:
                view = context.get("model_payload", context)
                return [
                    {"kind": kind, **item}
                    for kind, values in (
                        ("global_memory", [view.get("global_memory")]),
                        ("course_memory", [view.get("course_memory")]),
                        ("concept_memory", view.get("concept_memories", [])),
                    ) for item in values if isinstance(item, dict) and item.get("content")
                ]
            items = []
            if context.get("student_memory"):
                items.append({"kind": "student_memory", **context["student_memory"]})
            items.extend({"kind": "session_experience", **item} for item in context.get("session_experiences", []))
            return items
        profile = self.profile(course_id, concept_ids, student_id)
        return [
            m
            for m in profile["memories"]
            if not concept_ids
            or m["concept_id"] is None
            or m["concept_id"] in concept_ids
        ][:8]

    @staticmethod
    def apply_evidence(
        db,
        *,
        concept_ids: list[str],
        attempt_id: str,
        question_id: str,
        diagnosis: dict,
        assisted: bool,
        student_id: str = "local",
        evidence_ledger: Iterable[tuple[str, Sequence[MasteryEvidence]]] | None = None,
    ) -> list[dict]:
        """在调用方的事务中，把合格作答转换为有界的掌握估计变化。

        辅助作答、无法判分、诊断置信度不足 0.8 或同题重复都不增加证据。
        首次证据初始化估计，后续按结果限制增减方向与步长，并依据独立题数
        限制掌握度上限；返回实际应用的变化，而不是模型建议的原始数值。
        """
        if evidence_ledger is not None:
            # 评分包与来源校验由评估域负责；学生域只归约不可写的账本投影。
            return apply_ledger_evidence(
                db, evidence_by_concept=evidence_ledger,
                student_id=student_id, attempt_id=attempt_id,
            )
        # 兼容旧单概念调用；缺少冻结局部回执时绝不广播整题结论。
        if len(set(concept_ids)) != 1:
            return []
        if (
            assisted
            or diagnosis["result"] == "ungraded"
            or diagnosis["confidence"] < 0.8
        ):
            return []
        changes = []
        level = diagnosis["level"]
        for cid in concept_ids:
            identifier = student_id + ":" + cid
            state = db.get(StudentState, identifier, with_for_update=True)
            if state is None:
                state = StudentState(
                    id=identifier,
                    concept_id=cid,
                    student_id=student_id,
                    mastery=dict.fromkeys(LEVELS),
                    evidence_ids=[],
                    evidence_questions=[],
                    misconceptions=[],
                    version=0,
                )
                db.add(state)
            # 同题重复作答保存过程，但不会叠加成多份独立掌握证据。
            if question_id in state.evidence_questions:
                continue
            before = dict(state.mastery)
            updated = dict(before)
            current = updated.get(level)
            if current is None:
                current = (
                    0.5
                    if diagnosis["result"] == "correct"
                    else 0.25 if diagnosis["result"] == "partial" else 0.0
                )
            else:
                proposed = float(diagnosis.get("suggested_delta", 0))
                # 反思提案只能沿证据支持的方向局部更新，不能因模型任意数值跳跃。
                if diagnosis["result"] == "correct":
                    delta = max(0.0, min(0.2, proposed))
                elif diagnosis["result"] == "partial":
                    delta = max(-0.05, min(0.05, proposed))
                else:
                    delta = max(-0.2, min(0.0, proposed))
                current = max(0.0, min(1.0, current + delta))
            count = len(state.evidence_questions) + 1
            # 一题最多支持 0.5、两题最多支持 0.8，避免少量证据直接宣称完全掌握。
            updated[level] = round(
                min(current, 0.5 if count == 1 else 0.8 if count == 2 else 1.0), 3
            )
            state.mastery = updated
            state.evidence_ids = [*state.evidence_ids, attempt_id]
            state.evidence_questions = [*state.evidence_questions, question_id]
            state.version += 1
            if diagnosis.get("misconception"):
                state.misconceptions = list(
                    dict.fromkeys([*state.misconceptions, diagnosis["misconception"]])
                )[-6:]
            state.next_review_at = datetime.now(timezone.utc).replace(
                tzinfo=None
            ) + timedelta(days=1 if diagnosis["result"] != "correct" else 3)
            changes.append(
                {
                    "concept_id": cid,
                    "before": before,
                    "after": updated,
                    "evidence_id": attempt_id,
                    "version": state.version,
                    "update_kind": (
                        "initialization" if before.get(level) is None else "incremental"
                    ),
                    "applied_delta": round(
                        updated[level] - (before.get(level) or 0), 3
                    ),
                }
            )
        return changes

    def forget(self, memory_id: str) -> None:
        """将记忆标记为失效，让后续教学不再检索它，保留原始审计记录。"""
        with db_session() as db:
            row = db.get(MemoryItem, memory_id)
            if row:
                row.status = "invalidated"
