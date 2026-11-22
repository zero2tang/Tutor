from app.domains.courses.difficulty.metadata import public_estimate

"""目录读取与公开题目序列化，答案始终留在服务端。"""

from collections import Counter
import re
from sqlalchemy import func, select

from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import Concept, Course, Material, Question
from app.domains.courses.questions.images import public_images, public_math_image
from app.domains.courses.questions.policy import access, require_access


def public_question(question: Question, *, assets=None) -> dict:
    """只投影公开题面；图示状态与独立练习能力由统一题库策略判断。"""
    validation = question.validation or {}
    images = public_images(question, assets)
    rights = access(question, assets=assets)
    availability, availability_note = rights.availability, rights.note
    blank_count = validation.get("blank_count")
    return {
        "id": question.id,
        "source_id": question.source_id,
        "course_id": question.course_id,
        "stem": question.stem,
        "stem_format": "markdown",
        "images": images,
        "options": question.options,
        "question_type": question.question_type,
        "cognitive_type": question.cognitive_type,
        "difficulty": question.difficulty,
        "difficulty_value": public_estimate(question).get("value"),
        "difficulty_source": public_estimate(question),
        "difficulty_target": validation.get("difficulty_target"),
        "concept_ids": question.concept_ids,
        "source_ref": question.source_ref,
        "generated": question.generated,
        "version": question.version,
        "teaching_pack_id": validation.get("teaching_pack_id"),
        "availability": availability,
        "availability_note": availability_note,
        **(
            {"blank_count": blank_count}
            if type(blank_count) is int and 1 <= blank_count <= 100
            else {}
        ),
    }


def question_contract(question: Question) -> dict:
    """在公开结构上附加答案与解析，形成仅供预测、判分和题解使用的内部契约。"""
    # 冻结契约仅用于内部预测与判分，不能直接通过 API 返回。
    validation = question.validation or {}
    criteria = []
    seen = set()
    raw_criteria = validation.get("assessment_criteria") or []
    if not isinstance(raw_criteria, list) or len(raw_criteria) > 32:
        raise ValueError("题目的逐知识点评分要点格式无效")
    for item in raw_criteria:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
            or not item["id"] or len(item["id"]) > 96 or item["id"] in seen
            or item.get("concept_id") not in question.concept_ids
            or item.get("dimension") not in {"beginner", "advanced", "proficient"}
            or not isinstance(item.get("description"), str) or not item["description"].strip()
            or len(item["description"]) > 2000):
            raise ValueError("题目的逐知识点评分要点不完整或超出题目范围")
        seen.add(item["id"])
        criteria.append({key: item[key] for key in ("id", "concept_id", "dimension", "description")})
    canonical = validation.get("canonical_item_id")
    if canonical is not None and (not isinstance(canonical, str) or not canonical.strip() or len(canonical) > 160):
        raise ValueError("题目的稳定身份标识无效")
    return {
        **public_question(question),
        "answer": question.answer,
        "explanation": question.explanation,
        "assessment_criteria": criteria,
        # 无显式身份时由评分服务按实质题干计算指纹，不能回退成可变题号。
        "canonical_item_id": canonical,
    }


class CatalogService:
    def course(self, identifier: str) -> dict:
        """以实际导入的课程记录校验范围，拒绝模型或请求虚构的课程 ID。"""
        with db_session() as db:
            row = db.get(Course, identifier)
            if row is None:
                raise ValueError("课程不存在，请先完成课程数据导入")
            return {
                "id": row.id,
                "title": row.title,
                "description": row.description,
                "source_ref": row.source_ref,
            }

    def courses(self) -> list[dict]:
        """返回已导入课程及知识点、原题、材料数量，不把生成题计入原题规模。"""
        with db_session() as db:
            courses = db.scalars(select(Course)).all()
            return [
                {
                    "id": c.id,
                    "title": c.title,
                    "description": c.description,
                    "source_ref": c.source_ref,
                    "concept_count": db.scalar(
                        select(func.count())
                        .select_from(Concept)
                        .where(Concept.course_id == c.id)
                    ),
                    "question_count": db.scalar(
                        select(func.count())
                        .select_from(Question)
                        .where(Question.course_id == c.id, Question.generated == False)
                    ),
                    "material_count": db.scalar(
                        select(func.count())
                        .select_from(Material)
                        .where(Material.course_id == c.id)
                    ),
                }
                for c in courses
            ]

    def concepts(self, course_id: str = "mechanics", query: str = "") -> list[dict]:
        """列出当前课程知识点及关联题数，可按名称过滤。

        当前实现读取本课题目后按 concept_ids 在 Python 计数；即使仅筛选
        少量知识点，也不意味着底层只读取这些知识点对应的题目。
        """
        self.course(course_id)
        with db_session() as db:
            statement = (
                select(Concept)
                .where(Concept.course_id == course_id)
                .order_by(Concept.module, Concept.name)
            )
            if query:
                # 原 MySQL 名称列忽略大小写；PG 需显式保留相同检索语义。
                statement = statement.where(Concept.name.icontains(query))
            questions = db.scalars(
                select(Question).where(Question.course_id == course_id)
            ).all()
            counts = Counter(cid for q in questions for cid in q.concept_ids)
            return [
                {
                    "id": c.id,
                    "name": c.name,
                    "module": c.module,
                    "summary": c.summary,
                    "topic_type": c.topic_type,
                    "question_count": counts[c.id],
                }
                for c in db.scalars(statement)
            ]

    def concept(self, identifier: str, course_id: str = "mechanics") -> dict:
        """校验单个知识点归属，附上最多 4 份公开材料供 Agent 获取教学依据。

        当前材料先按课程读取、再按 concept_ids 在 Python 筛选；返回数量
        上限只限制输出，不代表数据库仅查询 4 行。
        """
        self.course(course_id)
        with db_session() as db:
            row = db.get(Concept, identifier)
            if row is None or row.course_id != course_id:
                raise ValueError("知识点不存在或不属于当前课程")
            materials = [
                m
                for m in db.scalars(
                    select(Material).where(
                        Material.course_id == course_id, Material.visibility == "public"
                    )
                )
                if identifier in m.concept_ids
            ]
            return {
                "id": row.id,
                "name": row.name,
                "module": row.module,
                "topic_type": row.topic_type,
                "summary": row.summary,
                "materials": [
                    {
                        "id": m.id,
                        "title": m.title,
                        "text": m.text,
                        "source_ref": m.source_ref,
                        "origin": m.origin,
                    }
                    for m in materials[:4]
                ],
            }

    def question(
        self, identifier: str, course_id: str = "mechanics", *, internal: bool = False
    ) -> dict:
        """按 ID 和课程读取题目；默认返回公开结构，内部服务才显式请求答案契约。"""
        self.course(course_id)
        with db_session() as db:
            row = db.get(Question, identifier)
            if row is None or row.course_id != course_id:
                raise ValueError("题目不存在或不属于当前课程")
            require_access(row, "solution" if internal else "read",
                solution_authorized=internal, allow_archived_solution=internal)
            return question_contract(row) if internal else public_question(row)

    def resolve_concepts(
        self,
        query: str,
        course_id: str = "mechanics",
        requested: list[str] | None = None,
    ) -> list[dict]:
        """将已知 ID 或文本中的知识点名称解析为本课目录对象。

        显式 ID 必须全部有效；文本路径仅做名称包含匹配并优先长名称，
        不等同于语义理解，也不负责决定用户的真实学习意图。
        """
        available = self.concepts(course_id)
        if requested:
            selected = [c for c in available if c["id"] in requested]
            if len(selected) != len(set(requested)):
                raise ValueError("目标中包含未知或其他课程知识点")
            return selected
        selected = [c for c in available if c["name"].casefold() in query.casefold()]
        return sorted(selected, key=lambda c: len(c["name"]), reverse=True)[:5]
