"""用短事务占用和释放会话；模型执行期间只保留任务标识。"""

from app.infrastructure.persistence.database import db_session
from app.domains.sessions.models import LearningSession


class SessionLease:
    def __init__(self, catalog, *, database=None):
        self.catalog = catalog
        self.database = database or db_session

    def claim(self, session_id: str, turn_id: str, *, answering: bool = False) -> dict:
        """占用本轮会话并返回可信上下文快照。

        通过行锁检查 active_turn_id，避免同一会话同时执行两个任务。
        事务结束后保留任务标识，模型推理期间不持续持有数据库行锁。
        """
        with self.database() as db:
            row = db.get(LearningSession, session_id, with_for_update=True)
            if not row:
                raise ValueError("会话不存在")
            if row.active_turn_id:
                raise ValueError("当前会话已有任务正在执行，请等待完成")
            if answering and not row.pending_question_id:
                raise ValueError("当前没有等待作答的题目")
            # 课程范围以持久化会话为准，不能由模型工具参数改变。
            self.catalog.course(row.course_id)
            row.active_turn_id = turn_id
            if answering:
                row.phase = "assessing"
            return {
                "goal": row.goal,
                "course_id": row.course_id,
                "student_id": row.student_id,
                "concept_ids": list(row.concept_ids),
                "pending_question_id": row.pending_question_id,
                "pending_context": dict(row.pending_context),
            }

    def release(self, session_id: str, turn_id: str, *, failed: bool = False) -> None:
        """仅释放本轮持有的任务标识，并按待答、澄清或失败情况恢复会话阶段。"""
        with self.database() as db:
            row = db.get(LearningSession, session_id, with_for_update=True)
            if row and row.active_turn_id == turn_id:
                row.active_turn_id = None
                row.phase = (
                    "awaiting_answer"
                    if row.pending_question_id
                    or (
                        row.pending_context.get("guided_teaching", {}).get("status")
                        in {"awaiting_student", "awaiting_clarification"}
                        and not row.pending_context.get("guided_teaching", {}).get(
                            "closed"
                        )
                    )
                    else (
                        "interrupted"
                        if failed
                        else (
                            "awaiting_clarification"
                            if row.pending_context.get("decision")
                            else "learning"
                        )
                    )
                )
