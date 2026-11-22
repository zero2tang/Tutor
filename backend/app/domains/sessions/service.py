"""会话事实持久化，刷新页面时直接恢复原题与目标。"""

from __future__ import annotations

from uuid import uuid4
from sqlalchemy import select

from app.infrastructure.persistence.database import db_session
from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.sessions.models import LearningSession, Message
from app.domains.courses.catalog import CatalogService
from app.domains.practice.context import public_exercise
from app.domains.practice.batch_state import public_batch
from app.shared.execution import LearningTurnLost
from app.domains.sessions.goals import ensure_session_goals, public_goals


def message_dict(row: Message) -> dict:
    """把持久化消息转换为接口字段，保留角色、引用和恢复界面所需的元数据。"""
    return {
        "id": row.id,
        "role": row.role,
        "agent": row.agent,
        "content": row.content,
        "citations": row.citations,
        # 下划线字段是服务端私有快照，不能随 HTTP 或 SSE 消息发给浏览器。
        "metadata": {
            key: value
            for key, value in (row.metadata_json or {}).items()
            if not key.startswith("_")
        },
        "created_at": row.created_at.isoformat(),
    }


class SessionService:
    def create(
        self,
        goal: str,
        concept_ids: list[str],
        course_id: str = "mechanics",
        student_id: str = "local",
    ) -> dict:
        """建立绑定课程和学生的学习会话，仅预绑定用户明确传入的知识点。

        自然语言目标的解释留给主 Agent；初建会话不会凭目标文本推断掌握度。
        """
        catalog = CatalogService()
        catalog.course(course_id)
        # 只预绑定用户明确选中的 ID，自然语言目标由主 Agent 自主查询和理解。
        resolved = (
            catalog.resolve_concepts("", course_id, concept_ids) if concept_ids else []
        )
        with db_session() as db:
            row = LearningSession(
                id=str(uuid4()),
                course_id=course_id,
                student_id=student_id,
                title=goal[:70],
                goal=goal,
                concept_ids=[c["id"] for c in resolved],
                agenda=[],
                pending_context={},
            )
            db.add(row)
            db.flush()
            ensure_session_goals(db, row)
            identifier = row.id
        return self.get(identifier)

    def list(self, course_id: str | None = None) -> list[dict]:
        """返回本机个人档案最近 50 个会话摘要，可限定课程，不加载整段对话。"""
        if course_id:
            CatalogService().course(course_id)
        with db_session() as db:
            statement = select(LearningSession).where(
                LearningSession.student_id == "local"
            )
            if course_id:
                statement = statement.where(LearningSession.course_id == course_id)
            return [
                {
                    "id": s.id,
                    "course_id": s.course_id,
                    "title": s.title,
                    "goal": s.goal,
                    "phase": s.phase,
                    "concept_ids": s.concept_ids,
                    "updated_at": s.updated_at.isoformat(),
                }
                for s in db.scalars(
                    statement.order_by(LearningSession.updated_at.desc()).limit(50)
                )
            ]

    def get(self, identifier: str) -> dict:
        """根据数据库事实恢复会话、消息、待澄清问题和公开待答题。

        含答案的冻结契约保留在后端，只序列化界面需要的状态，避免依靠模型
        重新生成题目或从对话摘要猜测尚未完成的交互。
        """
        with db_session() as db:
            row = db.get(LearningSession, identifier, with_for_update=True)
            if row is None:
                raise ValueError("会话不存在")
            messages = list(
                db.scalars(
                    select(Message)
                    .where(Message.session_id == identifier)
                    .order_by(Message.created_at, Message.id)
                )
            )
            if row.course_id == "junior_algebra":
                # 数据库时间精度可能只有秒；用事务内写入的版本与序号恢复教学顺序。
                messages.sort(
                    key=lambda message: (
                        tuple(
                            (message.metadata_json or {}).get("_tutoring_order", [0, 0])
                        ),
                        message.created_at,
                        message.id,
                    )
                )
            context = row.pending_context
            from app.domains.guidance.workspace import public_workspace

            data = {
                "id": row.id,
                "course_id": row.course_id,
                "title": row.title,
                "goal": row.goal,
                "concept_ids": row.concept_ids,
                "agenda": row.agenda,
                "learning_progress": row.pending_context.get("learning_progress"),
                "session_goals": public_goals(ensure_session_goals(db, row)),
                "pending_decision": row.pending_context.get("decision"),
                "phase": row.phase,
                "version": row.version,
                "archived_problem": {
                    key: context["archived_problem"].get(key)
                    for key in ("question_id", "stem", "status", "notice")
                } if context.get("archived_problem") else None,
                "guidance": public_workspace(context.get("guided_teaching")),
                "active_turn_id": row.active_turn_id,
                "messages": [message_dict(m) for m in messages],
                "pending_question": None,
                "pending_batch": (
                    public_batch(context["exercise_batch"], row.course_id)
                    if context.get("exercise_batch")
                    else None
                ),
                "pending_assisted": bool(context.get("assisted", False)),
                "created_at": row.created_at.isoformat(),
            }
            if row.pending_question_id and not context.get("exercise_batch"):
                data["pending_question"] = (
                    public_exercise(context["contract"], row.course_id)
                    if context.get("contract")
                    else CatalogService().question(
                        row.pending_question_id, row.course_id
                    )
                )
                data["pending_question"]["practice_type"] = context.get(
                    "cognitive_type", "understanding"
                )
            from app.domains.sessions.interactions import pending_interaction
            data["pending_interaction"] = pending_interaction(row, messages)
            return data

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        agent: str = "",
        citations: list | None = None,
        metadata: dict | None = None,
        turn_id: str | None = None,
    ) -> dict:
        """在短事务中保存消息及业务元数据，提交后返回可供事件推送的消息结构。"""
        with db_session() as db:
            if turn_id is not None:
                session = db.get(LearningSession, session_id, with_for_update=True)
                if session is None or session.active_turn_id != turn_id:
                    raise LearningTurnLost("任务不再持有当前会话，不能提交过时消息")
            row = Message(
                id=str(uuid4()),
                session_id=session_id,
                role=role,
                agent=agent,
                content=content,
                citations=citations or [],
                metadata_json=metadata or {},
            )
            db.add(row)
            db.flush()
            from app.domains.memory.recording.facts import record_message_fact

            record_message_fact(db, row)
            return message_dict(row)

    def runs(self, session_id: str) -> list[dict]:
        """返回最近 40 次 Agent 执行的角色、工具轨迹、耗时和 Tokens 等审计摘要。"""
        with db_session() as db:
            rows = db.scalars(
                select(AgentRun)
                .where(AgentRun.session_id == session_id)
                .order_by(AgentRun.created_at.desc())
                .limit(40)
            ).all()
            return [
                {
                    "id": r.id,
                    "turn_id": r.turn_id,
                    "agent": r.agent,
                    "skill": r.skill,
                    "status": r.status,
                    "duration_ms": round(r.duration_ms, 2),
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "tool_calls": ([{k:v for k,v in item.items() if k in {"name", "status", "duration_ms", "tool_call_id", "call_index", "cached"}} for item in r.tool_calls] if r.agent == "exercise" else r.tool_calls),
                    "summary": "习题内部任务" if r.agent == "exercise" else r.output_summary,
                    "error": ("内部任务失败" if r.error else None) if r.agent == "exercise" else r.error,
                }
                for r in rows
            ]

    def recover(self) -> None:
        """后端启动时释放遗留任务占用并标记中断，保留待答题而不自动重跑模型。"""
        with db_session() as db:
            for row in db.scalars(
                select(LearningSession).where(
                    LearningSession.active_turn_id.is_not(None)
                )
            ):
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
                    else "interrupted"
                )
            for run in db.scalars(select(AgentRun).where(AgentRun.status == "running")):
                run.status, run.error = "interrupted", "后端重启中断，请恢复会话后重试"
