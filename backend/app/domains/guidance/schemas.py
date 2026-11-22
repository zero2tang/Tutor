"""讲题请求与模型教学动作；业务身份和真实学习证据由服务端维护。"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.shared.contracts import StrictModel





class ExternalProblem(StrictModel):
    stem: str = Field(min_length=2, max_length=8000)
    source_kind: Literal["pasted", "recognized"] = "pasted"

    @model_validator(mode="after")
    def nonempty(self):
        self.stem = self.stem.strip()
        if len(self.stem) < 2:
            raise ValueError("请提供完整的习题内容。")
        return self


class GuidanceRequest(StrictModel):
    action: Literal["start", "reply", "hint", "explain", "transfer", "close"]
    problem: ExternalProblem | None = None
    text: str = Field(default="", max_length=5000)
    request_id: str = Field(min_length=8, max_length=80)
    expected_version: int | None = Field(default=None, ge=0)
    wait_id: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def executable(self):
        if self.action == "start" and self.problem is None:
            raise ValueError("开始新题需要提供题干。")
        if self.action != "start" and self.problem is not None:
            raise ValueError("继续讲题不能替换原题，请先结束当前工作区。")
        if self.action == "reply" and not self.text.strip():
            raise ValueError("请先写出想法、作答或具体追问。")
        return self


class Technique(StrictModel):
    name: str = Field(max_length=160)
    applicability: str = Field(default="", max_length=600)
    origin: Literal["textbook", "worked_example", "model_inference"] = "model_inference"
    source_ids: list[str] = Field(default_factory=list, max_length=12)


class SolutionPath(StrictModel):
    name: str = Field(max_length=160)
    foundations: list[str] = Field(default_factory=list, max_length=12)
    techniques: list[str] = Field(default_factory=list, max_length=12)
    outline: str = Field(default="", max_length=1800)


class RelatedResource(StrictModel):
    resource_id: str = Field(max_length=300)
    relation: Literal["exact", "structural", "method", "diagnostic", "concept"]
    reason: str = Field(max_length=600)


class TeachingAnalysis(StrictModel):
    summary: str = Field(min_length=1, max_length=1500)
    foundations: list[str] = Field(default_factory=list, max_length=16)
    techniques: list[Technique] = Field(default_factory=list, max_length=10)
    solution_paths: list[SolutionPath] = Field(default_factory=list, max_length=5)
    related_resources: list[RelatedResource] = Field(
        default_factory=list, max_length=12
    )
    # 这是模型形成的参考解，不宣称已由程序证明；不序列化到公共工作区。
    reference_solution: str = Field(default="", max_length=5000)
    uncertainties: list[str] = Field(default_factory=list, max_length=12)
    source_ids: list[str] = Field(default_factory=list, max_length=20)


class SupportAnnotation(StrictModel):
    # 标签只帮助审计具体透露了什么；是否算帮助由服务端发布边界决定。
    kind: Literal[
        "attention_cue",
        "concept_explanation",
        "model_structure",
        "method",
        "worked_step",
        "unclassified_teaching",
    ]
    excerpt: str = Field(min_length=1, max_length=1800)


class TransferProblem(StrictModel):
    stem: str = Field(min_length=2, max_length=3000)
    reference_solution: str = Field(min_length=1, max_length=3000)
    target: str = Field(max_length=500)
    help_level: int = Field(
        default=0,
        ge=0,
        le=3,
        description="仅指针对这道新题已提供的帮助，不包含原题的示范。",
    )
    publication_mode: Literal["independent", "assisted"] = Field(
        default="independent",
        description=(
            "independent由服务端仅发布题干和中性邀请，模型自由前导文字不发布；"
            "assisted允许发布提示，服务端至少计一级帮助。"
        ),
    )
    teaching_content: str = Field(
        default="",
        max_length=3000,
        description="只针对新题的提示；原题示范不得放入此字段。独立发布时不公开。",
    )
    support_annotations: list[SupportAnnotation] = Field(
        default_factory=list, max_length=12
    )


class GuidedTeachingOutput(StrictModel):
    # 正文仅包含本轮需要展示的解释；参考解和诊断假设分开保存。
    action: Literal[
        "clarify", "ask", "explain", "hint", "worked_example", "transfer", "complete"
    ]
    content: str = Field(min_length=5, max_length=7000)
    response_expectation: Literal["answer", "optional", "none"] = Field(
        default="answer",
        description="answer要求回答question；optional仅邀请回应；none表示解释后自然停顿。",
    )
    question: str = Field(default="", max_length=1500)
    focus: str = Field(default="", max_length=500)
    help_level: int = Field(default=0, ge=0, le=3)
    support_annotations: list[SupportAnnotation] = Field(
        default_factory=list,
        max_length=12,
        description="从实际公开正文摘录帮助片段，建模结构也必须标记；不决定独立性。",
    )
    analysis: TeachingAnalysis | None = None
    student_input_kind: Literal["attempt", "question", "acknowledgement", "other"] = (
        Field(
            default="other",
            description=(
                "对本次学生原话的模型分类，不是正确性证明；只有实际作答或有内容的推理才是attempt，"
                "追问与‘懂了’不能当作attempt。"
            ),
        )
    )
    observation: str = Field(default="", max_length=1200)
    understanding: Literal["unknown", "needs_help", "partial", "demonstrated"] = (
        "unknown"
    )
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    transfer_problem: TransferProblem | None = None
