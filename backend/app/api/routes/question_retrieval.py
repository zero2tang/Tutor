"""查询入口使用持久化会话范围；未提供上传、复核或索引发布的公共写接口。"""

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from app.api.dependencies import services
from app.domains.courses.questions.search.schemas import Intent
from app.domains.courses.questions.search.models import QuestionAsset
from app.domains.courses.questions.search.store import asset_dict
from app.infrastructure.persistence.database import db_session

router = APIRouter(prefix="/api")


class ExerciseQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query_text: str = Field(default="", max_length=20000)
    intent: Intent = "isomorphic"
    asset_ids: list[str] = Field(default_factory=list, max_length=10)
    source_question_id: str | None = Field(default=None, max_length=96)
    top_k: int = Field(default=6, ge=1, le=20)


@router.post("/sessions/{session_id}/exercise-search")
async def exercise_search(session_id: str, body: ExerciseQuery, request: Request):
    session = services(request)["sessions"].get(session_id)
    # 本地单用户应用沿用既有会话入口；用户、课程和排除条件不由模型请求填写。
    with db_session() as db:
        from app.domains.sessions.models import LearningSession
        row = db.get(LearningSession, session_id)
        student_id = row.student_id
    return await services(request)["retrieval"].retrieve(body.query_text, kind="questions", course_id=session["course_id"],
        session_id=session_id, student_id=student_id, intent=body.intent, asset_ids=body.asset_ids,
        source_question_id=body.source_question_id, top_k=body.top_k)


@router.get("/assets/exercises/{asset_id}")
def exercise_asset(asset_id: str, request: Request):
    service = services(request)["question_retrieval"]
    with db_session() as db:
        row = db.get(QuestionAsset, asset_id)
        if row is None:
            raise HTTPException(404, "题图不存在")
        asset = asset_dict(row)
    try:
        from app.application.question_retrieval.assets import public_asset_pixels
        public_asset_pixels(asset_id, asset["question_id"], asset["course_id"], store=service.store, reader=service.reader)
    except (ValueError, OSError):
        raise HTTPException(404, "题图已撤回或已变化")
    return FileResponse(asset["location"], headers={"Cache-Control": "no-store"})
