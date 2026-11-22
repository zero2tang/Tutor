"""模型只能提出有来源的条件；人工复核状态由业务服务写入。"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Intent = Literal["exact", "isomorphic", "same_method"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(StrictModel):
    asset_id: str | None = None
    region: list[float] | None = None
    evidence_kind: Literal["explicit_text", "explicit_diagram_marker", "readable_value", "unknown"]
    quote: str = Field(max_length=2000)

    @model_validator(mode="after")
    def region_is_normalized(self):
        if self.asset_id is not None:
            b = self.region
            if not b or len(b) != 4 or not (0 <= b[0] < b[2] <= 1 and 0 <= b[1] < b[3] <= 1):
                raise ValueError("图示证据必须关联真实资产与归一化区域")
        elif self.region is not None:
            raise ValueError("没有资产的文字来源不能伪造图片坐标")
        return self


class GroundedFact(StrictModel):
    text: str = Field(min_length=1, max_length=2000)
    relation: str = Field(default="", max_length=120)
    objects: list[str] = Field(default_factory=list, max_length=30)
    evidence: Evidence


class VisualExtraction(StrictModel):
    ocr: list[GroundedFact] = Field(default_factory=list, max_length=120)
    formulas: list[GroundedFact] = Field(default_factory=list, max_length=80)
    conditions: list[GroundedFact] = Field(default_factory=list, max_length=100)
    goals: list[GroundedFact] = Field(default_factory=list, max_length=40)
    subquestions: list[str] = Field(default_factory=list, max_length=40)
    image_roles: dict[str, list[Literal["whole_question", "necessary", "formula", "context", "decoration", "unknown"]]] = Field(default_factory=dict)
    unknowns: list[str] = Field(default_factory=list, max_length=60)


class Match(StrictModel):
    question_id: str
    match_kind: Literal["exact", "isomorphic", "same_method", "topic_only", "unrelated"]
    relevance: int = Field(ge=0, le=3)
    evidence: list[str] = Field(default_factory=list, max_length=12)
    differences: list[str] = Field(default_factory=list, max_length=12)
    unknowns: list[str] = Field(default_factory=list, max_length=12)


class RerankOutput(StrictModel):
    matches: list[Match] = Field(max_length=20)
