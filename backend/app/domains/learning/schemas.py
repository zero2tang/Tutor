"""业务输入输出协议；校验器和字段约束与迁移前一致。"""

from typing import Annotated, Literal
from pydantic import Field, model_validator
from app.shared.contracts import StrictModel
from app.domains.practice.schemas import ExerciseBatchRequest
from app.domains.sessions.schemas import SessionGoalUpdate


class Clarification(StrictModel):
    objective: str = Field(max_length=1000)
    missing_information: str = Field(max_length=400)
    options: list[str] = Field(default_factory=list, max_length=5)


class LearningContinuation(StrictModel):
    # 仅记录教学流程执行进度，不作为学生掌握度或能力评估证据。
    status: Literal["awaiting_student", "awaiting_clarification", "completed"]
    current_step: str = Field(min_length=1, max_length=240)
    next_step: str = Field(min_length=1, max_length=500)
    completed_steps: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(
        default_factory=list, max_length=6
    )


class Plan(StrictModel):
    # 主 Agent 的最终决策契约；action 由 LearningService 分发，不是工具名称。
    action: Literal["teach", "practice", "explain", "review", "clarify", "respond"] = (
        Field(
            description="teach=知识点讲解、概念解释或原理推导；practice=出题；explain=已有习题的题解；review=知识复习；clarify=向用户澄清；respond=直接沟通或推荐说明。"
        )
    )
    concept_ids: list[str] = Field(max_length=6)
    message: str = Field(max_length=2000)
    steps: list[Annotated[str, Field(max_length=120)]] = Field(max_length=6)
    rationale: str = Field(max_length=400)
    clarification: Clarification | None = None
    goal_update: str | None = Field(default=None, max_length=1000)
    goal_update_request: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description="只有用户明确要求更换整体学习目标时填写该要求的当前消息原话；阶段推进或措辞润色不更换目标。",
    )
    continuation: LearningContinuation | None = Field(
        default=None,
        description="持久保存当前教学步骤、下一步和已执行步骤；这是学习流程记录，不代表学生已经掌握。",
    )
    session_goal_update: SessionGoalUpdate | None = Field(
        default=None,
        description="本会话内目标图的增量提案。使用session_goals.version作为expected_version并保留稳定goal_id；补基础用push_remediation，结束后用resume。整体目标更换仍只用goal_update。",
    )
    exercise_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=36,
        description="讨论或讲解历史练习时指定上下文/工具返回的出题记录ID；不是题库题号。省略时使用当前讨论题。",
    )
    exercise_batch: ExerciseBatchRequest | None = Field(
        default=None,
        description="按学习需要请求1至6道习题，数量由tasks长度决定，多题并行生成；practice直接执行，teach/review在讲解后执行。每题指定知识点、认知类型、作答题型与可选难度目标。",
    )

    @model_validator(mode="after")
    def executable_target(self):
        """在 JSON 解析后检查教学类动作必须有目标；跨课程和目录校验由规划器追加。"""
        if self.action in {"teach", "practice", "review"} and not self.concept_ids:
            raise ValueError(
                "开展教学或练习前需要具体知识点。可调用 search_concepts 或先澄清，不能随意填入目录根节点。"
            )
        return self


class Lesson(StrictModel):
    # 知识点 Agent 的输出，服务层负责组装标题、正文与后续思考题。
    title: str
    content: str = Field(min_length=30)
    follow_up: str
    next_action: str
    exercise_batch: ExerciseBatchRequest | None = Field(
        default=None,
        description="讲解后需要学生完成一组练习时填写，由服务端并行调用习题Agent。已有未答练习时不得请求新题。",
    )


class MessageRequest(StrictModel):
    # 页面传入的请求偏好与幂等标识，不等同于主 Agent 已确认的最终行动。
    content: str = Field(min_length=1, max_length=5000)
    intent: Literal["auto", "teach", "practice", "explain", "review"] = "auto"
    cognitive_type: Literal["recall", "understanding", "application"] = "understanding"
    generate: bool = False
    exercise_count: int | None = Field(default=None, ge=1, le=6)
    concept_ids: list[str] = Field(default_factory=list, max_length=6)
    request_id: str = Field(min_length=8, max_length=80)
    wait_id: str | None = Field(default=None, min_length=1, max_length=120)


class SkipRequest(StrictModel):
    request_id: str = Field(min_length=8, max_length=80)
    wait_id: str = Field(min_length=1, max_length=120)
