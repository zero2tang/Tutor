"""教学资料业务入口：检索与读取共用访问边界，工具层仅适配模型调用。"""

from app.infrastructure.persistence.database import db_session
from app.domains.materials.resources.access import ResourceAccess
from app.domains.materials.resources.search import search_resources
from app.domains.materials.resources.read import read_resource


class TeachingResourceService:
    def __init__(self):
        self.access = ResourceAccess()

    def search(
        self,
        course_id,
        query,
        kind="all",
        limit=6,
        *,
        excluded_question_ids=(),
        session_id=None,
        student_id="local",
    ):
        return search_resources(
            self.access,
            db_session,
            course_id,
            query,
            kind,
            limit,
            excluded_question_ids=excluded_question_ids,
            session_id=session_id,
            student_id=student_id,
        )

    def read(
        self,
        course_id,
        resource_id,
        *,
        excluded_question_ids=(),
        session_id=None,
        student_id="local",
    ):
        return read_resource(
            self.access,
            db_session,
            course_id,
            resource_id,
            excluded_question_ids=excluded_question_ids,
            session_id=session_id,
            student_id=student_id,
        )
