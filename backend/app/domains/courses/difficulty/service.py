"""只读每题唯一难度；不计算知识点加权分数或比较多个配置。"""

import math
from sqlalchemy import select
from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import Course, Concept, Question
from app.domains.courses.questions.policy import require_access, searchable_conditions
from .metadata import estimate, public_estimate


class DifficultyQueryService:
    def __init__(self, session_factory=None):
        self.session_factory = session_factory or db_session

    def detail(self, course_id, *, entity_id, entity_type="question"):
        if entity_type != "question":
            raise ValueError("知识点聚合难度已退役，仅支持题目难度")
        with self.session_factory() as db:
            question = db.get(Question, entity_id)
            if question is None or question.course_id != course_id:
                raise ValueError("题目不属于当前课程")
            require_access(question)
            return {
                "course_id": course_id,
                "entity_type": "question",
                "entity_id": entity_id,
                **public_estimate(question),
            }

    def query(
        self,
        course_id,
        *,
        entity_type="question",
        concept_ids=None,
        min_difficulty=0,
        max_difficulty=100,
        order="desc",
        limit=10,
        offset=0,
        excluded_question_ids=None,
        query=""
    ):
        if (
            entity_type != "question"
            or order not in {"asc", "desc"}
            or not 1 <= limit <= 20
            or not 0 <= offset <= 10000
        ):
            raise ValueError("仅支持题目难度排序，次序或分页参数无效")
        if (
            any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in (min_difficulty, max_difficulty)
            )
            or not 0 <= min_difficulty <= max_difficulty <= 100
        ):
            raise ValueError("难度区间必须满足0≤最小值≤最大值≤100")
        with self.session_factory() as db:
            if db.get(Course, course_id) is None:
                raise ValueError("课程不存在")
            for cid in concept_ids or []:
                concept = db.get(Concept, cid)
                if concept is None or concept.course_id != course_id:
                    raise ValueError("筛选知识点不属于当前课程")
            stmt = select(Question).where(
                Question.course_id == course_id, *searchable_conditions(Question)
            )
            if excluded_question_ids:
                stmt = stmt.where(Question.id.not_in(excluded_question_ids))
            if query.strip():
                # 与原 MySQL 题干列一致，英文关键词大小写不影响筛选。
                stmt = stmt.where(Question.stem.icontains(query.strip()))
            rows = []
            scored = 0
            for question in db.scalars(stmt):
                if concept_ids and not set(concept_ids).intersection(
                    question.concept_ids
                ):
                    continue
                value = estimate(question)
                if value.get("value") is None:
                    continue
                scored += 1
                if min_difficulty <= value["value"] <= max_difficulty:
                    rows.append((question, value))
            rows.sort(
                key=lambda pair: (
                    (-pair[1]["value"] if order == "desc" else pair[1]["value"]),
                    pair[0].id,
                )
            )
            from app.domains.courses.catalog import public_question

            selected = rows[offset : offset + limit]
            return {
                "course_id": course_id,
                "entity_type": "question",
                "available": bool(selected),
                "items": [
                    {
                        **public_question(q),
                        "value": v["value"],
                        "difficulty_value": v["value"],
                        "status": v["status"],
                        "difficulty_source": public_estimate(q),
                    }
                    for q, v in selected
                ],
                "scored_count": scored,
                "matched_count": len(rows),
                "offset": offset,
                "returned_count": len(selected),
                "has_more": offset + len(selected) < len(rows),
                "notice": "每课程只保留一种估计来源；不同课程不混排，未知与过期不填0。",
            }
