"""维护可恢复的学习流程进度，不产生学生掌握度证据。"""

from copy import deepcopy

from app.domains.learning.schemas import LearningContinuation

from app.domains.sessions.progress import preserve_learning_context


def save_progress(row, continuation: dict | None) -> dict | None:
    """借用调用方已有会话与事务保存进度，不创建数据库连接或修改能力状态。"""
    if continuation is None:
        # 兼容尚未输出 continuation 的旧主 Agent 契约。
        return None
    update = LearningContinuation.model_validate(continuation).model_dump()
    pending = dict(row.pending_context or {})
    previous = pending.get("learning_progress")
    previous = previous if isinstance(previous, dict) else {}
    completed = []
    if previous.get("objective") == row.goal:
        completed = [
            step
            for step in previous.get("completed_steps", [])
            if isinstance(step, str) and step.strip() and len(step) <= 240
        ]
    # 目标变更后旧执行步骤不属于新目标；同一目标仅合并去重并保留最后24项。
    completed = list(dict.fromkeys([*completed, *update["completed_steps"]]))[-24:]
    progress = {"objective": row.goal, **update, "completed_steps": completed}
    row.pending_context = {**pending, "learning_progress": progress}
    return deepcopy(progress)
