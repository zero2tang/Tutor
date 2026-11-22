"""模型只能提出目标变更；会话归属、版本与执行状态由服务校验。"""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field

GoalId = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_:-]+$")]
GoalStatus = Literal["planned", "active", "blocked", "awaiting_student", "completed", "cancelled"]
ShortText = Annotated[str, Field(min_length=1, max_length=400)]


class GoalNode(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    goal_id: GoalId
    parent_id: GoalId | None = None
    description: str = Field(min_length=1, max_length=1000)
    source_ref: str = Field(min_length=1, max_length=140)
    success_criteria: list[ShortText] = Field(min_length=1, max_length=8)
    status: GoalStatus = "planned"
    prerequisites: list[GoalId] = Field(default_factory=list, max_length=12)
    evidence_refs: list[Annotated[str, Field(min_length=1, max_length=140)]] = Field(default_factory=list, max_length=24)


class SessionGoalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: int = Field(ge=0)
    nodes: list[GoalNode] = Field(default_factory=list, max_length=24)
    active_goal_id: GoalId | None = None
    next_action: str = Field(default="", max_length=1000)
    operation: Literal["update", "push_remediation", "resume"] = "update"
    remediation_goal_id: GoalId | None = Field(
        default=None,
        description="push_remediation时指定补救节点。服务自动保存原节点、原题和步骤返回点；resume不能提交自造返回栈。",
    )
