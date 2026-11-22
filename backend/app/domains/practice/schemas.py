"""业务输入输出协议；校验器和字段约束与迁移前一致。"""

from typing import Annotated, Literal
from pydantic import Field, model_validator
from app.shared.contracts import StrictModel


class PracticeRequest(StrictModel):
    question_id: str = Field(min_length=1, max_length=96)
    cognitive_type: Literal["recall", "understanding", "application"] = "understanding"
    request_id: str = Field(min_length=8, max_length=80)


class DifficultyRange(StrictModel):
    # 这是出题目标，不能冒充已经过量表标注的实际题目难度。
    minimum: float = Field(ge=0, le=100)
    maximum: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def ordered(self):
        if self.minimum > self.maximum:
            raise ValueError("难度范围下限不能大于上限")
        return self


class ExerciseTask(StrictModel):
    # 单个子任务只描述考查范围，课程、学生和会话身份由服务端注入。
    concept_ids: list[str] = Field(min_length=1, max_length=3)
    cognitive_type: Literal["recall", "understanding", "application"] = "understanding"
    question_type: Literal["choice", "multi_choice", "fill"] = "choice"
    difficulty_range: DifficultyRange | None = Field(
        default=None,
        description="可选的内容难度目标，0至100，越大越难；未独立标注前不能宣称实际难度达标。",
    )
    instruction: str = Field(default="", max_length=500)


class ExerciseBatchRequest(StrictModel):
    title: str = Field(default="巩固练习", min_length=1, max_length=100)
    tasks: list[ExerciseTask] = Field(min_length=1, max_length=6)


class ExerciseSelection(StrictModel):
    # 只允许提出候选题号；是否属于候选集由习题服务再次检查。
    question_id: str
    reason: str


class ExerciseDraft(StrictModel):
    # 生成草稿包含私有答案，只有通过服务校验与独立求解后才能保存和公开出题。
    stem: str = Field(min_length=15)
    question_type: Literal["choice", "multi_choice", "fill"] = "choice"
    options: dict[str, str]
    explanation: str = Field(
        description="先完成公开推导、代入检验及数量域检查，再填写最终answer；发现计算或题目错误时不得留下未修正的答案栏。"
    )
    answer: list[str] = Field(
        description="完成解析与检验后的最终答案，必须与explanation的最后结论一致。"
    )
    concept_ids: list[str]
    cognitive_type: Literal["recall", "understanding", "application"]


class SolutionCheck(StrictModel):
    # 独立求解调用的输出，不向求解上下文提供生成草稿的参考答案。
    explanation: str = Field(
        description="先独立求解原题并检验，不得改动题干、选项或添加整数等未声明条件。"
    )
    answer: list[str] = Field(
        description="依据已完成推导填写最终答案，不能与explanation或issue中的结论矛盾。"
    )
    valid: bool
    issue: str


class SolutionAnswerAssessment(StrictModel):
    # 原样回执绑定实际答案栏，不能用解析里另一个数值冒充输入最终答案。
    submitted_answer: list[str] = Field(
        description="原样复制被审查解答的完整answer列表，不改格式、不替换成解析结论。"
    )
    reason: str = Field(min_length=1, max_length=2000)
    correctness: Literal["correct", "incorrect", "uncertain"] = Field(
        description="评价submitted_answer本身是否正确；明确标注的部分回答按其声明范围判断，错误最终答案不能当作正确部分回答。"
    )
    answer_explanation_consistency: Literal[
        "consistent", "inconsistent", "uncertain"
    ] = Field(
        description="检查实际answer与该份解析结论是否一致，不能仅因解析正确就判答案栏正确。"
    )


class SolutionReview(StrictModel):
    # 先审查两份真实输入并给理由，最后才输出用于判分的统一答案。
    generation_assessment: SolutionAnswerAssessment
    independent_assessment: SolutionAnswerAssessment
    reason: str = Field(min_length=1, max_length=2000)
    explanation: str = Field(max_length=5000)
    verdict: Literal[
        "equivalent",
        "complementary",
        "repair_required",
        "conflict",
        "invalid",
        "uncertain",
    ]
    answer: list[Annotated[str, Field(min_length=1, max_length=1500)]] = Field(
        max_length=4,
        description="只在两份输入答案均正确且各自与解析一致、并满足接受条件时填写。需要修复、冲突、无效或无法确认时必须为空列表。",
    )

    @model_validator(mode="after")
    def accepted_solution_is_complete(self):
        if self.verdict in {"equivalent", "complementary"}:
            if any(
                assessment.correctness != "correct"
                or assessment.answer_explanation_consistency != "consistent"
                for assessment in (
                    self.generation_assessment,
                    self.independent_assessment,
                )
            ):
                raise ValueError(
                    "任一输入答案错误、与自身解析矛盾或无法确认时不能接受；应返回repair_required、conflict或uncertain，且answer为空。"
                )
            if (
                not self.answer
                or any(not item.strip() for item in self.answer)
                or not self.explanation.strip()
            ):
                raise ValueError("等价或互补的结论必须给出统一后的有效答案和解析")
        elif self.answer:
            raise ValueError(
                "需要修复、冲突、无效或无法确认时不提供可用于判分的最终答案，应说明原因"
            )
        return self


class Explanation(StrictModel):
    content: str
    key_points: list[str]
