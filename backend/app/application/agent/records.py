"""用短事务记录 Agent 运行；模型和工具等待期间不持有数据库事务。"""

import asyncio
from sqlalchemy import select,func
from app.domains.sessions.models import LearningSession

from app.infrastructure.persistence.execution_models import AgentRun


class SQLRunRecorder:
    def __init__(self, database):
        self.database = database

    def start(self, run_id, role, skill, context, turn_id, model):
        with self.database() as db:
            request_id=getattr(context,"request_id",None)
            if context.session_id and request_id:
                # 会话行锁串行化同一请求的运行登记，不为费用预算新增表。
                session=db.get(LearningSession,context.session_id,with_for_update=True)
                if session is None or session.course_id!=context.course_id or session.student_id!=context.student_id:
                    raise PermissionError("模型费用归属与会话身份不一致")
                count=db.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.session_id==context.session_id,
                    AgentRun.private_diagnostics["request_id"].as_string()==request_id))
                if count>=64:raise ValueError("同一请求的模型运行总预算已耗尽，请使用已有结果")
            db.add(
                AgentRun(
                    id=run_id,
                    session_id=context.session_id,
                    turn_id=turn_id,
                    agent=role,
                    skill=skill,
                    model=model,
                    status="running",
                    private_diagnostics={"request_id":request_id} if request_id else {},
                )
            )

    def complete(self, run_id, duration, usage, trace, output):
        with self.database() as db:
            run = db.get(AgentRun, run_id)
            run.status, run.duration_ms = "completed", duration
            run.prompt_tokens, run.completion_tokens = (
                usage["prompt_tokens"],
                usage["completion_tokens"],
            )
            run.tool_calls = trace
            run.private_diagnostics = {
                **run.private_diagnostics,
                "usage_status": "partial_or_unknown" if usage.get("_incomplete") else "reported",
            }
            run.output_summary = "习题内部任务完成" if run.agent == "exercise" else str(
                output.get("title", output.get("message", output.get("feedback", "结构化任务完成")))
            )[:500]

    def fail(self, run_id, duration, usage, trace, error):
        with self.database() as db:
            run = db.get(AgentRun, run_id)
            run.status = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
            run.error = type(error).__name__ if run.agent == "exercise" else f"{type(error).__name__}: {str(error)[:250]}"
            run.duration_ms = duration
            run.prompt_tokens, run.completion_tokens = (
                usage["prompt_tokens"],
                usage["completion_tokens"],
            )
            run.tool_calls = trace
            # 整数列保留已报告量的下界；失败未报告用量不能解释成实际零消耗。
            run.private_diagnostics = {**run.private_diagnostics, "usage_status": "partial_or_unknown",
                **({"schema_rejections": error.validation_rejections} if hasattr(error, "validation_rejections") else {})}
