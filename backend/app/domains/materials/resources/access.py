"""资料访问范围、待答题排除、来源指纹与原文投影，供检索和读取共用。"""

from copy import deepcopy

from app.domains.materials.teaching_reference import pack_views
from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.domains.courses.models import Course
from app.domains.sessions.models import LearningSession
from app.domains.materials.models import TextbookDocument
from app.domains.materials.resources.projections import source_view


class ResourceAccess:
    """统一资料访问规则，避免检索与读取出现两套隔离逻辑。"""

    def scope(self, db, course_id, excluded_question_ids, session_id, student_id):
        if db.get(Course, course_id) is None:
            raise ValueError("课程不存在。")
        excluded = set(excluded_question_ids or [])
        if session_id:
            session = db.get(LearningSession, session_id)
            if (
                session is None
                or session.course_id != course_id
                or session.student_id != student_id
            ):
                raise PermissionError("资料读取不能改变课程、学生或会话范围。")
            if session.pending_question_id:
                excluded.add(session.pending_question_id)
            pending = session.pending_context or {}
            for item in (pending.get("exercise_batch") or {}).get("items", []):
                if item.get("contract", {}).get("id"):
                    excluded.add(item["contract"]["id"])
            # 退役的 equation_tutoring 只保留历史；不再占用当前待答范围。
        return excluded

    @staticmethod
    def documents(db, course_id):
        # 先排序轻量主键，再读取 JSON；避免 MySQL 对大教材快照做 filesort 耗尽排序内存。
        identifiers = list(
            db.scalars(
                select(TextbookDocument.id)
                .where(TextbookDocument.course_id == course_id)
                .order_by(TextbookDocument.created_at.desc(), TextbookDocument.id)
                .limit(100)
            )
        )
        rows = list(
            db.scalars(
                select(TextbookDocument)
                .options(
                    load_only(
                        TextbookDocument.id,
                        TextbookDocument.course_id,
                        TextbookDocument.title,
                        TextbookDocument.source_uri,
                        TextbookDocument.source_kind,
                        TextbookDocument.content_hash,
                        TextbookDocument.parsed,
                    )
                )
                .where(TextbookDocument.id.in_(identifiers))
            )
        )
        by_id = {row.id: row for row in rows}
        return [by_id[identifier] for identifier in identifiers]

    @staticmethod
    def packs(db, course_id):
        return sorted(pack_views(db, course_id), key=lambda row: (row.created_at, row.id), reverse=True)[:200]

    @staticmethod
    def private_block_ids(document):
        return {
            identifier
            for exercise in document.parsed.get("exercises", [])
            for identifier in exercise.get("source_block_ids", [])
            + exercise.get("solution_block_ids", [])
        }

    @staticmethod
    def refs(document, ids):
        blocks = {block["id"]: block for block in document.parsed.get("blocks", [])}
        return [
            {
                **source_view(blocks[identifier].get("source", {})),
                "block_id": identifier,
                "document_id": document.id,
            }
            for identifier in dict.fromkeys(ids)
            if identifier in blocks
        ]

    @staticmethod
    def archive(db, pack, course_id, excluded):
        if pack.course_id != course_id or pack.question_id in excluded:
            raise PermissionError("参考资料不属于当前课程或来自当前待答题。")
        document = db.get(TextbookDocument, pack.document_id)
        if document is None or document.course_id != course_id:
            raise ValueError("教材分析资料来源不可用。")
        if pack.payload.get("document_id") != document.id:
            raise ValueError("存档资料文档归属不一致。")
        if (
            pack.payload.get("provenance", {}).get("document_content_hash")
            != document.content_hash
        ):
            raise ValueError("教材原文版本与存档分析不一致。")
        return document

    @staticmethod
    def blocks(document, ids):
        blocks = {block["id"]: block for block in document.parsed.get("blocks", [])}
        return [
            {
                "block_id": identifier,
                "kind": blocks[identifier].get("kind"),
                "text": blocks[identifier].get("text", ""),
                "maths": deepcopy(blocks[identifier].get("maths", [])),
                "source": source_view(blocks[identifier].get("source", {})),
                "provenance": "textbook_transcription",
                "unread_image_count": len(blocks[identifier].get("images", [])),
            }
            for identifier in dict.fromkeys(ids)
            if identifier in blocks
        ]
