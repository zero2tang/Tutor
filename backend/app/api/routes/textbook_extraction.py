"""离线提取任务的只读结果接口，不接受服务端文件路径或模型写库指令。"""

from fastapi import APIRouter
from app.domains.materials.extraction import queries

router = APIRouter(prefix="/api/textbook-extraction")


@router.get("/books")
def books(course_id: str = "junior_math_7a"):
    return queries.list_books(course_id)


@router.get("/books/{book_id}/sections")
def sections(book_id: str, course_id: str = "junior_math_7a"):
    return queries.list_sections(book_id, course_id)


@router.get("/sections/{section_id}")
def section(section_id: str, course_id: str = "junior_math_7a"):
    return queries.public_section(section_id, course_id)


@router.get("/jobs/{job_id}")
def job(job_id: str, course_id: str = "junior_math_7a"):
    return queries.job_status(job_id, course_id)
