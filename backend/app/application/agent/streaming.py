"""教学输出的公开字段策略；解析器本身位于执行内核。"""

from app.agent.runtime.streaming import AgentTextStream as RuntimeTextStream, PublicJSONText
from app.domains.assessment.schemas import BatchDiagnosis, Diagnosis
from app.domains.practice.schemas import Explanation
from app.domains.learning.schemas import Lesson, Plan
from app.domains.guidance.schemas import GuidedTeachingOutput

PUBLIC_TEXT_FIELDS = {
    Plan: {"message"},
    Lesson: {"title", "content", "follow_up"},
    Explanation: {"content"},
    Diagnosis: {"feedback"},
    BatchDiagnosis: {"summary"},
    GuidedTeachingOutput: {"content"},
}


def public_fields(schema):
    """未知输出契约默认不产生公开正文增量。"""
    return frozenset(PUBLIC_TEXT_FIELDS.get(schema, ()))


class AgentTextStream(RuntimeTextStream):
    def __init__(self, schema, role, emit):
        super().__init__(public_fields(schema), role, emit)
