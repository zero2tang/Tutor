"""单题与练习组共用的出题要求，不在参数路由时丢失教学请求。"""

from app.domains.practice.schemas import ExerciseTask


def validate_generation_operation(operation: str, task_count: int | None) -> None:
    """工具入口和业务入口使用相同模式约束，防止直接调用绕过。"""
    if operation not in {"explain", "select", "generate", "batch"}:
        raise ValueError("不支持的习题操作")
    if operation == "batch" and task_count is None:
        raise ValueError("练习组需要exercise_batch逐题计划")
    if task_count is not None and operation not in {"generate", "batch"}:
        raise ValueError("exercise_batch仅能用于generate或batch操作")
    if operation == "generate" and task_count is not None and task_count != 1:
        raise ValueError("generate只支持一道题；多题请使用batch操作")


def generation_assignment(
    task: ExerciseTask | None = None,
    *,
    teaching_request: str = "",
    index: int = 1,
    total: int = 1,
) -> dict:
    """保留整体教学要求与逐题指令，避免将长请求塞入较短的instruction字段。"""
    return {
        "index": index,
        "total": total,
        "teaching_request": teaching_request,
        "instruction": task.instruction if task else "",
        "question_type": task.question_type if task else "choice",
        "difficulty_range": (
            task.difficulty_range.model_dump()
            if task and task.difficulty_range
            else None
        ),
        "instruction_for_diversity": "围绕本题考查要求独立设计条件与数值，不要照搬常见例题。",
    }
