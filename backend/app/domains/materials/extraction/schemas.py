"""模型只能提交有页内来源的资源；教学归纳与教材转录分别保存。"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Region(Contract):
    bbox: list[float] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def coordinates(self):
        x0, y0, x1, y1 = self.bbox
        if not (0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000):
            raise ValueError("来源坐标必须是左上原点的0—1000有序矩形。")
        return self


class Definition(Region):
    name: str
    text: str


class TeachingBlock(Region):
    role: Literal["introduction", "explanation", "question", "summary", "activity", "note"]
    text: str
    figure_ids: list[str] = Field(default_factory=list)


class Figure(Region):
    id: str
    caption: str | None = None
    description: str
    role: Literal["essential", "explanatory", "decorative", "uncertain"]


class Table(Contract):
    headers: list[str]
    rows: list[list[str]]


class Subquestion(Contract):
    label: str
    text: str


class Exercise(Region):
    number: str
    kind: Literal["example", "practice", "section_exercise", "review", "activity"]
    group: str | None = None
    stem: str
    subquestions: list[Subquestion] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    figure_ids: list[str] = Field(default_factory=list)
    answer: str | None = None
    solution: str | None = None
    solution_bbox: list[float] | None = None
    continued_from_previous: bool = False

    @model_validator(mode="after")
    def solution_coordinates(self):
        if self.solution_bbox is not None:
            Region(bbox=self.solution_bbox)
        return self


class Method(Region):
    name: str
    statement: str
    origin: Literal["textbook_explicit", "model_summarized"]
    conditions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)

    @field_validator("conditions", "steps", mode="before")
    @classmethod
    def scalar_list(cls, value):
        # 单条字符串包装为一项，不补写任何数学条件。
        return [value] if isinstance(value, str) else value


class PageExtraction(Contract):
    printed_page: str
    headings: list[str] = Field(default_factory=list)
    definitions: list[Definition] = Field(default_factory=list)
    teaching_blocks: list[TeachingBlock] = Field(default_factory=list)
    exercises: list[Exercise] = Field(default_factory=list)
    methods: list[Method] = Field(default_factory=list)
    figures: list[Figure] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class PageReview(Contract):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    corrected: PageExtraction | None = None


class TeachingStep(Contract):
    role: str
    description: str
    source_ids: list[str] = Field(min_length=1)


class TeachingDesign(Contract):
    summary: str
    objectives: list[str]
    steps: list[TeachingStep]
    key_questions: list[TeachingStep] = Field(default_factory=list)
    potential_difficulties: list[TeachingStep] = Field(default_factory=list)
    methods: list[TeachingStep] = Field(default_factory=list)

