"""题目难度只读 API；历史多口径构建入口已退役。"""

from fastapi import APIRouter, Query, HTTPException

from app.domains.courses.difficulty.service import DifficultyQueryService

router = APIRouter(prefix="/api/difficulty", tags=["difficulty"])


@router.get("/profiles")
def profiles(course_id: str = "mechanics"):
    raise HTTPException(
        status_code=410, detail="多口径难度配置已退役，请读取题目难度元数据。"
    )


@router.get("/search")
def search(
    course_id: str = "mechanics",
    entity_type: str = "question",
    concept_ids: str = "",
    min_difficulty: float = Query(0, ge=0, le=100),
    max_difficulty: float = Query(100, ge=0, le=100),
    order: str = "desc",
    limit: int = Query(10, ge=1, le=20),
    offset: int = Query(0, ge=0, le=10000),
    q: str = "",
):
    return DifficultyQueryService().query(
        course_id,
        entity_type=entity_type,
        concept_ids=[cid for cid in concept_ids.split(",") if cid],
        min_difficulty=min_difficulty,
        max_difficulty=max_difficulty,
        order=order,
        limit=limit,
        offset=offset,
        query=q,
    )


@router.get("/detail")
def detail(entity_id: str, entity_type: str, course_id: str = "mechanics"):
    return DifficultyQueryService().detail(
        course_id, entity_type=entity_type, entity_id=entity_id
    )
