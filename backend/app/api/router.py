"""按既有注册顺序装配全部 HTTP 接口，统一保留现有 /api URL。"""

from fastapi import APIRouter

from app.api.routes.health import router as health_router
from app.api.routes.courses import router as courses_router
from app.api.routes.retrieval import router as retrieval_router
from app.api.routes.sessions import router as sessions_router
from app.api.routes.learning import router as learning_router
from app.api.routes.students import router as students_router
from app.api.routes.assets import router as assets_router
from app.api.routes.difficulty import router as difficulty_router
from app.api.routes.textbooks import router as textbooks_router
from app.api.routes.guidance import router as guidance_router
from app.api.routes.memory import router as memory_router
from app.api.routes.textbook_extraction import router as textbook_extraction_router
from app.api.routes.question_retrieval import router as question_retrieval_router

router = APIRouter()

router.include_router(health_router)
router.include_router(courses_router)
router.include_router(retrieval_router)
router.include_router(sessions_router)
router.include_router(learning_router)
router.include_router(students_router)
router.include_router(assets_router)
router.include_router(difficulty_router)
router.include_router(textbooks_router)
router.include_router(guidance_router)
router.include_router(memory_router)
router.include_router(textbook_extraction_router)
router.include_router(question_retrieval_router)
