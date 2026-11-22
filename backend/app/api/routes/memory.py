"""记忆接口始终从会话归属推导学生范围，不接收任意 student_id。"""

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import Concept
from app.domains.sessions.models import LearningSession
from app.domains.memory.visibility import public_memory_snapshot

router = APIRouter(prefix="/api/sessions/{session_id}/memory", tags=["memory"])


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    close: bool = False


class CorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    kind: Literal["student", "global", "course", "concept", "experience", "all"] = (
        "student"
    )
    concept_id: str | None = None
    claim_id: str | None = None


class RecordingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["student", "global", "course", "concept"]
    concept_id: str | None = None
    enabled: bool


def _service(request: Request):
    return request.app.state.services["memory"]


@router.get("")
def read_memory(session_id: str, request: Request):
    snapshot = _service(request).read(session_id)
    # 目录名称由服务查询，模型不能为未知ID编造知识点名称。
    with db_session() as db:
        session = db.get(LearningSession, session_id)
        if session is None:
            raise ValueError("会话不存在")
        for field in (
            "concept_memories",
            "concept_observations",
            "session_concept_observations",
        ):
            for item in snapshot.get(field, []):
                concept = db.get(Concept, item.get("concept_id"))
                if concept is not None:
                    item["concept_name"] = concept.name
        snapshot = public_memory_snapshot(snapshot, session)
    return snapshot


@router.post("/refresh")
async def refresh_memory(
    session_id: str, body: RefreshRequest, request: Request, wait: bool = False
):
    result = await _service(request).refresh(session_id, close=body.close, wait=wait)
    return {**result, **read_memory(session_id, request)}


@router.post("/close")
async def close_memory(session_id: str, request: Request, wait: bool = False):
    result = await _service(request).refresh(session_id, close=True, wait=wait)
    return {**result, **read_memory(session_id, request)}


@router.delete("/{kind}")
def forget_memory(
    session_id: str,
    kind: Literal["student", "global", "course", "concept", "experience", "all"],
    request: Request,
    concept_id: str | None = None,
    claim_id: str | None = None,
    disable: bool = False,
):
    memory = _service(request)
    hierarchy = request.app.state.services.get("hierarchical_memory")
    if hierarchy is None or kind == "experience":
        memory.forget(session_id, kind)
        return read_memory(session_id, request)
    if kind == "all":
        snapshot = hierarchy.read(session_id)
        hierarchy.forget(session_id, "student", disable=disable)
        hierarchy.forget(session_id, "course", disable=disable)
        for item in snapshot.get("concept_memories", []):
            hierarchy.forget(
                session_id, "concept", concept_id=item["concept_id"], disable=disable
            )
        memory.forget(session_id, "experience")
    else:
        hierarchy.forget(
            session_id, kind, concept_id=concept_id, claim_id=claim_id, disable=disable
        )
    return read_memory(session_id, request)


@router.post("/correct")
async def correct_memory(
    session_id: str, body: CorrectionRequest, request: Request, wait: bool = False
):
    memory = _service(request)
    hierarchy = request.app.state.services.get("hierarchical_memory")
    if hierarchy is None or body.kind == "experience":
        result = await memory.correct(session_id, body.text, kind=body.kind, wait=wait)
        return {**result, **read_memory(session_id, request)}
    if body.kind == "all":
        raise ValueError("纠正需要指定学生、课程或知识点作用域")
    await hierarchy.correct(
        session_id,
        body.text,
        scope=body.kind,
        concept_id=body.concept_id,
        claim_id=body.claim_id,
        wait=wait,
    )
    result = await memory.refresh(session_id, wait=wait)
    return {**result, **read_memory(session_id, request)}


@router.post("/recording")
def set_recording(session_id: str, body: RecordingRequest, request: Request):
    hierarchy = request.app.state.services.get("hierarchical_memory")
    if hierarchy is None:
        raise ValueError("分层记忆尚未启用")
    hierarchy.set_enabled(
        session_id, body.enabled, scope=body.scope, concept_id=body.concept_id
    )
    return read_memory(session_id, request)
