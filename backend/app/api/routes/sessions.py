"""学习会话和持久运行记录查询接口。"""

from fastapi import APIRouter, HTTPException, Request

from app.api.dependencies import services
from app.api.schemas import SessionCreate

router = APIRouter(prefix="/api")


@router.get("/sessions")
def list_sessions(request: Request, course_id: str | None = None):
    return {"items": services(request)["sessions"].list(course_id)}


@router.post("/sessions")
def create_session(body: SessionCreate, request: Request):
    return services(request)["sessions"].create(
        body.goal, body.concept_ids, body.course_id, body.student_id
    )


@router.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request):
    return services(request)["sessions"].get(session_id)


@router.get("/sessions/{session_id}/runs")
def runs(session_id: str, request: Request):
    services(request)["sessions"].get(session_id)
    return {"items": services(request)["sessions"].runs(session_id)}


@router.get("/sessions/{session_id}/events", deprecated=True)
def events(session_id: str, request: Request):
    services(request)["sessions"].get(session_id)
    raise HTTPException(410, "历史事件接口已退役，请读取会话快照和正式消息恢复。")
