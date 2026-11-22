"""模型只提出局部观察，服务端验证来源、评分要点及正式准入。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AnswerSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote: str = Field(min_length=1, max_length=5000)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)


class ConceptGradingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    concept_id: str
    dimension: Literal["beginner", "advanced", "proficient"]
    criterion_id: str = Field(default="", max_length=96)
    result: Literal["correct", "partial", "incorrect", "not_observed", "ungraded"]
    score: float | None = Field(default=None, ge=0, le=1)
    confidence: float = Field(default=0.0, ge=0, le=1)
    source_span_refs: list[AnswerSpan] = Field(default_factory=list, max_length=8)
    rationale: str = Field(default="", max_length=2000)


class GradingPacket(BaseModel):
    # 持久化时另校验内容指纹，嵌套 JSON 也不能在诊断阶段被悄然改写。
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = 1
    question_id: str
    source_version: str
    canonical_item_id: str
    contract_fingerprint: str
    answer_fingerprint: str
    overall: dict
    help_snapshot: dict
    concepts: list[dict]
    run_id: str | None = None
    fingerprint: str
