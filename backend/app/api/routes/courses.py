"""课程目录和知识点查询接口。"""

from fastapi import APIRouter, Request

from app.api.dependencies import services
from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import IndexTask

router = APIRouter(prefix="/api")


@router.get("/courses")
def courses(request: Request):
    items = services(request)["catalog"].courses()
    with db_session() as db:
        for course in items:
            tasks = [
                db.get(IndexTask, f"{course['id']}:v1:{target}")
                for target in ["es", "neo4j"]
            ]
            course["index_status"] = {
                target: task.status if task else "pending"
                for target, task in zip(["es", "neo4j"], tasks, strict=True)
            }
            if course["id"] == "junior_algebra":
                # 小型核验题集由主库直接读取，不伪称已经建立外部检索索引。
                course["index_status"] = {"es": "not_required", "neo4j": "not_required"}
    return {"items": items}


@router.get("/concepts")
def concepts(request: Request, course_id: str = "mechanics", q: str = ""):
    return {"items": services(request)["catalog"].concepts(course_id, q)}


@router.get("/concepts/{identifier}")
def concept(identifier: str, request: Request, course_id: str = "mechanics"):
    return services(request)["catalog"].concept(identifier, course_id)
