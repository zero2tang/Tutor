"""工具执行的可信业务范围，由服务端创建。"""

from dataclasses import dataclass, field


@dataclass
class ToolContext:
    # 由业务服务构建的执行范围，不作为可由模型自由覆盖的工具参数。
    course_id: str = "mechanics"
    session_id: str | None = None
    concept_ids: list[str] = field(default_factory=list)
    excluded_question_ids: list[str] = field(default_factory=list)
    student_id: str = "local"
    # 父级明确提供的例题可与教学概念采用不同原始标签，仅授权题解，不扩展新题范围。
    explanation_exercise_ids: list[str] = field(default_factory=list)
    # 来自真实请求的费用归属，不能由模型工具参数覆盖。
    request_id: str | None = None

    # 仅服务端冻结的撤销屏障，不暴露给模型工具参数。
    memory_epoch: int | None = None
