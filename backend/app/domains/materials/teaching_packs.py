"""教材参考目录的只读查询：校验来源完整性，不计算或证明数学答案。"""

from copy import deepcopy

from .teaching_reference import get_pack, pack_views, digest as payload_hash, source_question_map

from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import Question
from app.domains.materials.models import TextbookDocument
from app.domains.materials.catalog_projection import verify_catalog_pack


def _verification(value):
    result = {
        "result": "not_checked",
        "method": "source_structure_integrity",
        "scope": "本次只校验来源、结构及指纹；未核验数学答案、题型解法或学生掌握状态。",
    }
    original = value.get("verification", {})
    if original.get("method") != "source_structure_integrity":
        result["archive_verification"] = deepcopy(original)
    return result


def public_pack(row, question_map: dict | None = None) -> dict:
    """保留前端字段契约；原文解答和历史生成步骤只供内部参考读取。"""
    value, question_map = row.payload, question_map or {}
    return {
        "id": row.id,
        "course_id": row.course_id,
        "document_id": row.document_id,
        "question_id": row.question_id,
        "status": row.status,
        "hash": row.payload_hash,
        "problem": deepcopy(value["problem"]),
        "source_refs": deepcopy(value.get("source_refs", [])),
        "concepts": deepcopy(value.get("concepts", [])),
        "techniques": deepcopy(value.get("techniques", [])),
        "solution_paths": [
            {
                "id": path["id"],
                "label": path["label"],
                "requirements": deepcopy(path.get("requirements", {})),
                "verification": _verification(path),
            }
            for path in value.get("solution_paths", [])
        ],
        "verification": _verification(value),
        "provenance": deepcopy(value.get("provenance", {})),
        "issues": [
            {"code": issue.get("code"), "message": issue.get("message", "需要复核")}
            for issue in value.get("issues", [])
        ],
        "related_examples": [
            {
                **{
                    key: deepcopy(item.get(key))
                    for key in (
                        "relation_type",
                        "reason",
                        "is_prerequisite",
                        "source_refs",
                    )
                },
                "question_id": question_map.get(
                    item.get("question_id"), item.get("question_id")
                ),
            }
            for item in value.get("related_examples", [])
        ],
        "notice": "教材原有答案和解析作为来源资料保存，未作数学正确性核验；历史生成分析保留原始provenance，本次完整性校验不构成教师审核。",
    }


class TeachingPackService:
    def _public(self, db, row):
        return public_pack(row, source_question_map(db, row.course_id, row.document_id))

    def _checked(self, db, identifier: str, course_id: str, *, row=None, documents=None):
        row = row or get_pack(db, identifier)
        if row is None or row.course_id != course_id:
            raise ValueError("教学参考目录不存在或不属于当前课程。")
        if documents is None:
            documents = {}
        if row.document_id not in documents:
            documents[row.document_id] = db.get(TextbookDocument, row.document_id)
        document = documents[row.document_id]
        if document is None or document.course_id != course_id:
            raise ValueError("教学来源不存在或归属不一致。")
        if document.content_hash != document.parsed.get("document", {}).get(
            "content_hash"
        ):
            raise ValueError("教材来源内容指纹与解析快照不一致。")
        verify_catalog_pack(row.payload, document.parsed)
        return row

    def get(self, identifier: str, course_id="junior_algebra") -> dict:
        with db_session() as db:
            return self._public(db, self._checked(db, identifier, course_id))

    def list(
        self, course_id="junior_algebra", document_id=None, query="", limit=30
    ) -> dict:
        with db_session() as db:
            rows = sorted(pack_views(db, course_id, document_id), key=lambda row: (row.created_at, row.id))
            if query:
                rows = [
                    row
                    for row in rows
                    if query.casefold()
                    in (row.payload["problem"]["stem"] + row.question_id).casefold()
                ]
            documents, maps = {}, {}
            for row in rows:
                if row.document_id not in maps:
                    maps[row.document_id] = source_question_map(db, course_id, row.document_id)
            return {
                "items": [public_pack(self._checked(db, row.id, course_id, row=row, documents=documents), maps[row.document_id])
                          for row in rows[:max(1, min(limit, 100))]],
                "total": len(rows),
                "channels_used": ["textbook_source_catalog"],
            }

    def internal(self, question_id: str, course_id="junior_algebra") -> dict:
        """供引导教学读取来源档案；不要求原答案，也不按数学题型分流。"""
        with db_session() as db:
            question = db.get(Question, question_id)
            if question is None or question.course_id != course_id:
                raise ValueError("题目不存在或不属于当前课程。")
            identifier = (question.validation or {}).get("teaching_pack_id")
            if not identifier:
                raise ValueError("本题尚无教材来源档案。")
            row = self._checked(db, identifier, course_id)
            if any(
                issue.get("code")
                in {
                    "missing_problem_source",
                    "unresolved_source",
                    "blocking_parse_issue",
                }
                or issue.get("severity") in {"blocking", "error"}
                for issue in row.payload.get("issues", [])
            ):
                raise ValueError("本题来源结构不完整，需修复后才能作教学参考。")
            return deepcopy(row.payload)

