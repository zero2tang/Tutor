"""教材结构与教学包接口；内部解答不通过公开查询返回。"""

from fastapi import APIRouter
from app.domains.materials.schemas import TextbookImportRequest
from app.domains.materials.teaching_packs import TeachingPackService
from app.domains.materials.textbooks import TextbookService

router = APIRouter(prefix="/api")


@router.post("/textbooks/import")
def import_textbook(body: TextbookImportRequest):
    return TextbookService().import_document(body)


@router.get("/textbooks")
def textbooks(course_id: str = "junior_algebra"):
    return TextbookService().list(course_id)


@router.get("/textbooks/{document_id}")
def textbook(document_id: str, course_id: str = "junior_algebra"):
    return TextbookService().get(document_id, course_id)


@router.get("/textbooks/{document_id}/blocks/{block_id}")
def textbook_block(document_id: str, block_id: str, course_id: str = "junior_algebra"):
    return TextbookService().block(document_id, block_id, course_id)


@router.get("/teaching-packs")
def teaching_packs(
    course_id: str = "junior_algebra",
    document_id: str | None = None,
    q: str = "",
    limit: int = 30,
):
    return TeachingPackService().list(course_id, document_id, q, limit)


@router.get("/teaching-packs/{pack_id}")
def teaching_pack(pack_id: str, course_id: str = "junior_algebra"):
    return TeachingPackService().get(pack_id, course_id)
