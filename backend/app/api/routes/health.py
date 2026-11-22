"""应用及外部依赖的健康状态接口。"""

from fastapi import APIRouter, Request
from sqlalchemy import select, text

from app.api.dependencies import services
from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import IndexTask

router = APIRouter(prefix="/api")


@router.get("/health")
async def health(request: Request):
    service = services(request)
    status = {}
    try:
        with db_session() as db:
            db.execute(text("SELECT 1"))
            status["postgresql"] = {"status": "ready"}
            grouped = {}
            for row in db.scalars(
                select(IndexTask).where(IndexTask.target.in_(["es", "neo4j", "question_search"]))
            ):
                grouped.setdefault(row.target, {})[(row.course_id+":"+row.namespace) if row.target == "question_search" else row.id.split(":", 1)[0]] = {
                    "status": row.status,
                    "details": row.details,
                }
            indices = {
                target: {
                    "status": (
                        "ready"
                        if all(item["status"] in {"ready", "completed"} for item in values.values())
                        else "building"
                    ),
                    "details": {"courses": values},
                }
                for target, values in grouped.items()
            }
    except Exception as exc:
        status["postgresql"], indices = {"status": "error", "reason": type(exc).__name__}, {}
    try:
        data = await service["search"].request("GET", "/")
        status["elasticsearch"] = {
            "status": "ready",
            "version": data["version"]["number"],
        }
    except Exception as exc:
        status["elasticsearch"] = {"status": "error", "reason": type(exc).__name__}
    try:
        await service["graph"].health()
        status["neo4j"] = {"status": "ready"}
    except Exception as exc:
        status["neo4j"] = {"status": "error", "reason": type(exc).__name__}
    return {
        "application": "stem-tutor",
        "status": (
            "ready"
            if all(v["status"] == "ready" for v in status.values())
            else "degraded"
        ),
        "services": status,
        "indices": indices,
        "model": service["models"].settings.llm_model,
        "embedding_model": service["models"].settings.embedding_model,
        "persistence": {
            "business": "postgresql", "checkpointer": "postgresql",
            "memory_store": "postgresql", "memory_workflow": "langgraph",
        },
    }
