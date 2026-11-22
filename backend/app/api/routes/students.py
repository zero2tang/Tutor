"""学生档案和既有学习记忆查询接口。"""

from fastapi import APIRouter, Request

from app.api.dependencies import services

router = APIRouter(prefix="/api")


@router.get("/student")
def student(request: Request, student_id: str = "local", course_id: str = "mechanics"):
    return services(request)["student"].profile(
        course_id=course_id, student_id=student_id
    )


@router.delete("/student/memories/{memory_id}")
def forget_memory(memory_id: str, request: Request):
    services(request)["student"].forget(memory_id)
    return {"status": "invalidated"}
