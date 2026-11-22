"""新题引导接口复用学习任务、后台运行及SSE事件通道。"""

from fastapi import APIRouter, Request

from app.api.dependencies import services
from app.api.streaming import execute_operation
from app.domains.guidance.schemas import GuidanceRequest

router = APIRouter(prefix="/api")


@router.post("/sessions/{session_id}/guidance")
async def guide(
    session_id: str, body: GuidanceRequest, request: Request, stream: bool = True
):
    return await execute_operation(
        request,
        session_id,
        lambda turn_id, emit: services(request)["learning"].execute(
            session_id, {"kind": "guidance_action", **body.model_dump()}, turn_id, emit
        ),
        stream=stream,
    )
