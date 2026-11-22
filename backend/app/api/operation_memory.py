"""将学习事实交给派生记忆任务，记忆失败不改变已提交的教学结果。"""

import logging

logger = logging.getLogger(__name__)


def capture_operation_memory(
    request, session_id: str, result: dict | None = None
) -> None:
    memory = request.app.state.services.get("memory")
    if memory is None:
        return
    try:
        result = result or {}
        plan = result.get("plan") or {}
        goal_update = plan.get("session_goal_update") or {}
        tutoring = result.get("tutoring") or {}
        guidance = result.get("guidance") or {}
        reason = "turn"
        if not result.get("replayed"):
            if plan.get("goal_update"):
                reason = "goal_changed"
            elif goal_update.get("operation") == "resume" or (
                result.get("verdict") or {}
            ).get("probe"):
                reason = "remediation_return"
            elif any(
                node.get("status") == "completed"
                for node in goal_update.get("nodes", [])
            ):
                reason = "goal_completed"
            elif tutoring.get("status") in {
                "ready_for_transfer",
                "independently_verified",
            }:
                reason = "teaching_stage_complete"
            elif guidance.get("closed") or guidance.get("status") == "completed":
                reason = "guidance_stage_complete"
        # 由业务状态决定优先时机；记忆内容仍全部由模型提取。
        memory.capture(session_id, reason=reason, force=reason != "turn")
    except Exception as exc:
        # 同事务事实和可恢复游标负责补偿；日志不包含学生原话和凭据。
        logger.warning(
            "记忆任务登记待恢复 session=%s error=%s", session_id, type(exc).__name__
        )
