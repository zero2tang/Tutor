"""教材提取结果公开读取；分页原始回执和参考答案留在审阅档案中。"""

from copy import deepcopy
from sqlalchemy import select
from app.infrastructure.persistence.database import db_session
from .models import ExtractedSection
from app.domains.materials.models import TextbookDocument
from .store import ExtractionStore


def list_books(course_id):
    with db_session() as db:
        rows = db.scalars(select(TextbookDocument).where(TextbookDocument.course_id == course_id, TextbookDocument.format == "pdf", TextbookDocument.page_count > 0)).all()
        return {"items": [{"id": r.id, "title": r.title, "course_id": r.course_id,
                           "source_hash": r.source_hash, "page_count": r.page_count, "manifest": r.manifest} for r in rows]}


def list_sections(book_id, course_id):
    store = ExtractionStore()
    book = store.book(book_id)
    if book.course_id != course_id:
        raise ValueError("教材不属于当前课程。")
    return {"items": [{"id": r.id, "number": r.number, "title": r.title, "status": r.status,
                       "page_start": r.page_start, "page_end": r.page_end, "hash": r.payload_hash,
                       "coverage": r.payload.get("coverage", {}), "review": r.review} for r in store.sections(book_id)]}


def public_section(identifier, course_id):
    store = ExtractionStore()
    row = store.section(identifier)
    if store.book(row.book_id).course_id != course_id:
        raise ValueError("小节不属于当前课程。")
    payload = row.payload
    exercises = []
    for item in payload["exercises"]:
        exercises.append({key: deepcopy(value) for key, value in item.items()
                          if key not in {"answer", "solution", "solution_bbox", "solution_refs"}})
    return {"id": row.id, "book_id": row.book_id, "status": row.status, "hash": row.payload_hash,
            "number": row.number, "title": row.title, "definitions": payload["definitions"],
            "teaching_design": payload["teaching_design"], "methods": payload["methods"],
            "exercises": exercises, "figures": payload["figures"], "issues": payload["issues"],
            "provenance": payload["provenance"], "review": row.review}


def job_status(identifier, course_id):
    with db_session() as db:
        row = db.get(ExtractedSection, identifier)
        if row is None:
            raise ValueError("任务不存在。")
        book = db.get(TextbookDocument, row.book_id)
        if book.course_id != course_id:
            raise ValueError("任务不属于当前课程。")
        return {"id": row.id, "status": row.extraction_status, "attempts": row.attempts, "error": row.error, "receipt": row.receipt}
