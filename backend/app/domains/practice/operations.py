"""生成与发布的稳定身份；正式回执与业务效果共享原有事务。"""

from app.domains.sessions.models import LearningSession
from app.shared.execution import LearningTurnLost


def operation_key(context, operation_id: str | None, purpose: str) -> str:
    identifier = operation_id or context.request_id
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("习题写入必须提供稳定的请求或操作标识")
    return f"{purpose}:{identifier}"


def require_owner(db, context, turn_id, *, expected_version=None):
    """没有执行权时拒绝写入；不能让两个空 owner 被误认为相同。"""
    row = db.get(LearningSession, context.session_id, with_for_update=True)
    if (
        not turn_id
        or row is None
        or row.course_id != context.course_id
        or row.student_id != context.student_id
        or row.active_turn_id != turn_id
    ):
        raise LearningTurnLost("习题任务不再持有当前会话执行权")
    if expected_version is not None and row.version != expected_version:
        raise LearningTurnLost("习题任务的会话版本已变化，不能提交过时候选")
    from app.domains.memory.context import invalidation_epoch
    if context.memory_epoch is not None and invalidation_epoch(db, context.student_id, lock=True) != context.memory_epoch:
        raise ValueError("习题任务使用的记忆已撤销，拒绝旧候选。")
    return row
