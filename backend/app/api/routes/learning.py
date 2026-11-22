"""学习消息、练习及作答操作入口。"""

from fastapi import APIRouter, Request

from app.api.dependencies import services
from app.domains.practice.schemas import PracticeRequest
from app.domains.learning.schemas import SkipRequest
from app.api.streaming import execute_operation
from app.domains.assessment.schemas import AnswerRequest, BatchAnswerRequest
from app.domains.learning.schemas import MessageRequest

router = APIRouter(prefix="/api")


@router.post("/sessions/{session_id}/messages")
async def message(
    session_id: str, body: MessageRequest, request: Request, stream: bool = True
):
    # 自然语言请求进入主 Agent 规划与分发流程；HTTP 层不自行决定教学角色。
    learning = services(request)["learning"]
    return await execute_operation(
        request,
        session_id,
        lambda turn_id, emit: learning.turn(session_id, body, turn_id, emit),
        stream=stream,
    )


@router.post("/sessions/{session_id}/practice")
async def practice(
    session_id: str, body: PracticeRequest, request: Request, stream: bool = True
):
    return await execute_operation(request, session_id,
        lambda turn_id, emit: services(request)["learning"].execute(session_id,
            {"kind": "practice_existing", **body.model_dump()}, turn_id, emit), stream=stream)


@router.post("/sessions/{session_id}/answers")
async def answer(
    session_id: str, body: AnswerRequest, request: Request, stream: bool = True
):
    # 答案交给服务完成幂等、评估和证据提交，页面不能直接写掌握度。
    learning = services(request)["learning"]
    return await execute_operation(
        request,
        session_id,
        lambda turn_id, emit: learning.answer(
            session_id, body, turn_id, emit
        ),
        stream=stream,
    )


@router.post("/sessions/{session_id}/skip")
async def skip(session_id: str, body: SkipRequest, request: Request):
    return await execute_operation(request, session_id,
        lambda turn_id, emit: services(request)["learning"].execute(session_id,
            {"kind": "skip", **body.model_dump()}, turn_id, emit), stream=False)


@router.post("/sessions/{session_id}/batch-answers")
async def batch_answers(
    session_id: str, body: BatchAnswerRequest, request: Request, stream: bool = True
):
    learning = services(request)["learning"]
    return await execute_operation(
        request,
        session_id,
        lambda turn_id, emit: learning.answer_batch(session_id, body, turn_id, emit),
        stream=stream,
    )
