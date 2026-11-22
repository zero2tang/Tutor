"""v2 内部事件复用公共请求字段，仅增加有类型的业务动作。"""

from typing import Annotated, Literal, Union
from pydantic import Field, TypeAdapter
from app.domains.practice.schemas import PracticeRequest
from app.domains.learning.schemas import SkipRequest
from app.domains.assessment.schemas import AnswerRequest, BatchAnswerRequest
from app.domains.guidance.schemas import GuidanceRequest
from app.domains.learning.schemas import MessageRequest


class MessageEvent(MessageRequest):
    kind: Literal["message", "followup", "clarify", "change_goal"]


class AnswerEvent(AnswerRequest):
    kind: Literal["answer"]


class BatchAnswerEvent(BatchAnswerRequest):
    kind: Literal["batch_answer"]


class PracticeEvent(PracticeRequest):
    kind: Literal["practice_existing"]


class GuidanceEvent(GuidanceRequest):
    kind: Literal["guidance_action"]


class SkipEvent(SkipRequest):
    kind: Literal["skip"]


INPUT_EVENT = TypeAdapter(Annotated[Union[MessageEvent, AnswerEvent, BatchAnswerEvent,
    PracticeEvent, GuidanceEvent, SkipEvent], Field(discriminator="kind")])


def validate_event(value):
    # 显式偏好与默认值不同，不能在持久化时把所有默认字段变成显式要求。
    return INPUT_EVENT.validate_python(value).model_dump(exclude_unset=True)
