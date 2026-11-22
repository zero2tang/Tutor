"""业务输入输出协议；校验器和字段约束与迁移前一致。"""

from typing import Literal
from pydantic import Field
from app.shared.contracts import StrictModel
from app.domains.assessment.grading_schemas import ConceptGradingResult


class Diagnosis(StrictModel):
    # 评估 Agent 提供判分解释与修正建议；result、level、delta 仍受业务服务约束。
    result: Literal["correct", "partial", "incorrect", "ungraded"]
    score: float | None = Field(default=None, ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    feedback: str
    misconception: str
    prerequisite_hypothesis: str
    level: Literal["beginner", "advanced", "proficient"]
    suggested_delta: float = Field(ge=-0.25, le=0.25)
    reflection: str
    next_action: Literal["continue", "remediate", "advance", "clarify"]
    concept_results: list["ConceptGradingResult"] = Field(
        default_factory=list,
        max_length=30,
        description="逐知识点局部结果。综合题只能引用当前冻结契约assessment_criteria中的稳定criterion_id，且给出学生原答的准确source_span_refs；没有局部依据则not_observed，不能复制整题分数。",
    )


class BatchItemDiagnosis(StrictModel):
    question_id: str
    diagnosis: Diagnosis


class BatchDiagnosis(StrictModel):
    items: list[BatchItemDiagnosis] = Field(min_length=1, max_length=6)
    summary: str = Field(min_length=1, max_length=3000)
    next_action: Literal["continue", "remediate", "advance", "clarify"]


class AnswerRequest(StrictModel):
    # 幂等标识保证网络重试可以回放同一次提交，不能用于提交不同答案。
    answer: str = Field(min_length=1, max_length=5000)
    request_id: str = Field(min_length=8, max_length=80)
    wait_id: str = Field(min_length=1, max_length=120)
    question_id: str = Field(min_length=1, max_length=96)


class BatchAnswerItem(StrictModel):
    question_id: str = Field(min_length=1, max_length=96)
    answer: str = Field(min_length=1, max_length=5000)


class BatchAnswerRequest(StrictModel):
    batch_id: str = Field(min_length=1, max_length=36)
    answers: list[BatchAnswerItem] = Field(min_length=1, max_length=6)
    request_id: str = Field(min_length=8, max_length=80)
    wait_id: str = Field(min_length=1, max_length=120)
